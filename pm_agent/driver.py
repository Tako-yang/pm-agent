"""Driver Loop —— 外置循环控制权。

核心逻辑（详见 TDD §4.1）：
    for iter_n in range(MAX_ITER):
        if 终止条件: return
        if 预算降级阈值: pool.set_max_concurrent(1)
        decision = pm.decide_once()
        if guardrails.violates(decision): escalate; continue
        if 卡死: escalate; return
        execute(decision)
        save state

PM 只输出单轮决策，是否继续由 driver 决定。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pm_agent.concurrency import FileLockArbiter, WorkerPool, WorkerProgressMonitor
from pm_agent.cost import CostTracker
from pm_agent.escalation import EscalationStore
from pm_agent.guardrails import GuardrailsChecker
from pm_agent.memory import StructuredMemory
from pm_agent.pm import Decision, PMAgent, PMError
from pm_agent.process_group import cleanup_stale_workers
from pm_agent.project_store import ProjectStore
from pm_agent.utils import iso_now
from pm_agent.worktree import WorktreeManager

if TYPE_CHECKING:
    from pm_agent.workers.base import WorkerResult


class Driver:
    MAX_ITER = 200
    STUCK_THRESHOLD = 2
    DEFAULT_MAX_CONCURRENT = 3
    BUDGET_DEGRADE_RATIO = 0.7
    BUDGET_WARN_RATIO = 0.8

    def __init__(
        self,
        project_path: Path,
        max_concurrent: Optional[int] = None,
    ):
        self.project = ProjectStore(Path(project_path))
        # 启动时清理上次崩溃残留
        cleaned = cleanup_stale_workers(self.project.path)
        if cleaned:
            self.project.append_decision_log({
                "event": "startup_cleanup",
                "killed_stale_workers": cleaned,
            })

        self.cost_tracker = CostTracker(self.project.path)
        self.escalation = EscalationStore(self.project.path)
        self.guardrails = GuardrailsChecker(self.project)
        self.pm = PMAgent(self.project, guardrails=self.guardrails)

        # FileLockArbiter 必须先创建，WorkerPool 构造时注入
        self.file_lock_arbiter = FileLockArbiter(self.project)
        self.worker_pool = WorkerPool(
            max_concurrent=max_concurrent or self.DEFAULT_MAX_CONCURRENT,
            file_lock_arbiter=self.file_lock_arbiter,  # callback 自动 release
            cost_tracker=self.cost_tracker,            # callback 自动 add
        )
        self.progress_monitor = WorkerProgressMonitor(
            self.project, self.worker_pool
        )
        self.worktree_mgr = WorktreeManager(self.project.path)
        self.worktree_mgr.ensure_repo()

        self.last_decision_signature: Optional[tuple] = None
        self.stuck_count = 0
        # 缓存 task -> 上次失败时所用的 cli，用于失败重派切换 worker 类型
        self._failed_cli_history: dict[str, list[str]] = {}

    # ---------- 主循环 ----------

    def run(self) -> str:
        """运行 driver 直到终止条件触发。返回退出码字符串。"""
        try:
            return self._run_loop()
        finally:
            self.worker_pool.shutdown(wait=True)
            self.progress_monitor.shutdown()
            self._save_pool_state_to_disk()

    def _run_loop(self) -> str:
        for iter_n in range(self.MAX_ITER):
            self.project.save_state({"phase": "running", "iter": iter_n, "at": iso_now()})

            # === 终止条件检查 ===
            if self.project.is_done():
                self._notify("项目完成！")
                return "done"

            if self.cost_tracker.exceeded():
                self.worker_pool.kill_all()
                self.escalation.create(
                    title="预算耗尽",
                    body=f"累计 ${self.cost_tracker.total():.2f} 已超预算 ${self.cost_tracker.budget:.2f}",
                )
                self._notify("预算耗尽，已停止所有 worker")
                return "blocked_budget"

            if self.project.needs_human():
                # 等当前 worker 完成后退出
                self.worker_pool.wait_all(timeout=60)
                self.project.set_state("blocked_human")
                self._notify("等待 Boss 回复 escalation")
                return "blocked_human"

            # === 预算降级 ===
            ratio = self.cost_tracker.ratio()
            if ratio > self.BUDGET_DEGRADE_RATIO and self.worker_pool.max_concurrent > 1:
                self.worker_pool.set_max_concurrent(1)
                self.project.append_decision_log({
                    "event": "budget_degrade",
                    "ratio": ratio,
                    "new_max_concurrent": 1,
                })

            if ratio > self.BUDGET_WARN_RATIO:
                # 80% 告警 —— escalate 一次（去重靠 escalation 自身）
                self.escalation.create_once(
                    key="budget_80",
                    title="预算告警",
                    body=f"累计 ${self.cost_tracker.total():.2f} 已达预算 80%",
                )

            # === PM 单轮决策 ===
            try:
                recent_events = self._collect_recent_events()
                decision = self.pm.decide_once(
                    available_workers=self.worker_pool.available_slots(),
                    pool_status=self.worker_pool.status(),
                    recent_events=recent_events,
                )
            except PMError as e:
                self.escalation.create(title="PM 决策异常", body=str(e))
                self._notify(f"PM 决策异常: {e}")
                return "error"

            # === 护栏兜底校验 ===
            violations = self.guardrails.check(decision.to_dict())
            if violations:
                self.project.append_decision_log({
                    "event": "guardrails_violation",
                    "violations": violations,
                    "decision": decision.to_dict(),
                })
                self.escalation.create(
                    title="决策触发护栏",
                    body=f"违反条款: {violations}\n\n决策摘要:\n{decision.action}",
                    decision=decision.to_dict(),
                )
                continue

            # === 卡死检测（语义级模糊比较）===
            sig = self._signature(decision)
            if self._is_similar_decision(sig, self.last_decision_signature):
                self.stuck_count += 1
                if self.stuck_count >= self.STUCK_THRESHOLD:
                    self.escalation.create(
                        title="PM 决策卡死",
                        body=f"连续 {self.STUCK_THRESHOLD} 轮决策实质相同（签名: {sig}）",
                    )
                    self._notify("PM 卡死，已升级 Boss")
                    return "blocked_stuck"
            else:
                self.stuck_count = 0
            self.last_decision_signature = sig

            # === 执行决策 ===
            try:
                self._execute_decision(decision)
            except Exception as e:
                self.project.append_decision_log({
                    "event": "execute_decision_error",
                    "error": str(e),
                    "decision": decision.to_dict(),
                })
                self.escalation.create(title="决策执行异常", body=str(e))
                return "error"

            # === 持久化 ===
            self.project.tasks.save()
            self._save_pool_state_to_disk()

        self._notify(f"达到 MAX_ITER={self.MAX_ITER}，退出")
        return "max_iter_reached"

    # ---------- 决策执行 ----------

    def _execute_decision(self, decision: Decision) -> None:
        action = decision.action
        d = decision.to_dict()

        if action == "dispatch":
            submitted = self._dispatch_one(d)
            if submitted and self.worker_pool.active_count() > 0:
                # 修 BUG 2：只有 submit 成功才等任意完成，否则进下一轮 PM
                result = self.worker_pool.wait_any(
                    timeout=self.worker_pool.max_concurrent * 60
                )
                if result is not None:
                    tid, worker_result = result
                    self._on_worker_complete(tid, worker_result)
        elif action == "dispatch_parallel":
            self._dispatch_batch(d.get("batch", []))
        elif action == "verify":
            self._verify(d)
        elif action == "replan":
            self._replan(d)
        elif action == "complete":
            self.worker_pool.wait_all(timeout=300)
            self.project.set_state("done")
        elif action == "escalate":
            self.worker_pool.wait_all(timeout=60)
            self.escalation.create(
                title=d.get("escalation_title") or "PM 主动升级",
                body=d.get("escalation_reason", ""),
                decision=d,
            )
        elif action == "distill_memory":
            self.pm.distill_memory()
        else:
            raise ValueError(f"未知 action: {action}")

    def _dispatch_one(self, d: dict) -> bool:
        """派一个任务。返回 True=成功 submit, False=被锁/池满/不存在拒绝。

        主循环根据返回值决定是否调 wait_any（修 BUG 2：避免空 pool 上等空）。
        """
        task_id = d["task_id"]
        cli = d["cli"]
        prompt = d["prompt"]
        files_owned = d.get("files_owned", [])

        # 文件锁
        if not self.file_lock_arbiter.try_acquire(task_id, files_owned):
            self.project.append_decision_log({
                "event": "single_dispatch_rejected",
                "task_id": task_id,
                "reason": "file_lock_conflict",
            })
            return False

        # pool 满检查
        if self.worker_pool.available_slots() == 0:
            self.file_lock_arbiter.release(task_id)
            self.project.append_decision_log({
                "event": "single_dispatch_rejected",
                "task_id": task_id,
                "reason": "pool_full",
            })
            return False

        # 任务标记 running
        task = self.project.tasks.get(task_id)
        if task is None:
            self.file_lock_arbiter.release(task_id)
            self.project.append_decision_log({
                "event": "single_dispatch_rejected",
                "task_id": task_id,
                "reason": "task_not_found",
            })
            return False
        task.assigned_cli = cli
        if task.status != "running":
            try:
                task.transition("running")
            except ValueError:
                pass
        self.project.tasks.save()

        worktree = self.worktree_mgr.ensure_worktree(task_id)
        from pm_agent.workers.registry import WorkerRegistry
        dispatcher = WorkerRegistry.get(cli)
        try:
            self.worker_pool.submit(
                task_id=task_id,
                cli_name=cli,
                worker_fn=dispatcher.dispatch,
                worktree=worktree,
                timeout_sec=d.get("timeout_sec_override", 1800),
                no_output_timeout_sec=d.get("no_output_timeout_sec_override", 600),
                # worker_fn 的 keyword-only 参数：
                prompt=prompt,
                project_id=self.project.project_id,
            )
        except Exception as e:
            self.file_lock_arbiter.release(task_id)
            self.project.append_decision_log({
                "event": "submit_failed",
                "task_id": task_id,
                "error": str(e),
            })
            return False

        self.progress_monitor.watch(task_id, d.get("acceptance_check", ""))
        return True

    def _dispatch_batch(self, batch: list[dict]) -> None:
        """并发派发：partial-pass 策略。

        见 TDD §4.1.2：能派的派出去，不能派的记入 decision log，
        让 PM 下轮感知（"哪些被拒了，下次缩窄 files_owned"）。
        """
        approved: list[dict] = []
        rejected: list[str] = []

        for item in batch:
            tid = item["task_id"]
            files_owned = item.get("files_owned", [])
            if self.file_lock_arbiter.try_acquire(tid, files_owned):
                approved.append(item)
            else:
                rejected.append(tid)

        if rejected:
            self.project.append_decision_log({
                "event": "partial_batch_rejected",
                "rejected_task_ids": rejected,
                "approved_task_ids": [t["task_id"] for t in approved],
                "reason": "file_lock_conflict",
            })

        # 启动获批的任务
        for item in approved:
            tid = item["task_id"]
            cli = item["cli"]
            prompt = item["prompt"]
            task = self.project.tasks.get(tid)
            if task is None:
                self.file_lock_arbiter.release(tid)
                continue
            task.assigned_cli = cli
            if task.status != "running":
                try:
                    task.transition("running")
                except ValueError:
                    pass
            self.project.tasks.save()

            worktree = self.worktree_mgr.ensure_worktree(tid)
            from pm_agent.workers.registry import WorkerRegistry
            dispatcher = WorkerRegistry.get(cli)
            try:
                self.worker_pool.submit(
                    task_id=tid,
                    cli_name=cli,
                    worker_fn=dispatcher.dispatch,
                    worktree=worktree,
                    timeout_sec=item.get("timeout_sec_override", 1800),
                    no_output_timeout_sec=item.get("no_output_timeout_sec_override", 600),
                    prompt=prompt,
                    project_id=self.project.project_id,
                )
            except Exception as e:
                self.file_lock_arbiter.release(tid)
                self.project.append_decision_log({
                    "event": "submit_failed",
                    "task_id": tid,
                    "error": str(e),
                })
                continue
            self.progress_monitor.watch(tid, item.get("acceptance_check", ""))

        # 等任一 worker 完成或被 monitor kill，立即触发下一轮 PM 决策
        # 注意：approved 不等于实际 submit 成功——批内有任务可能被 pool 满拒绝
        if self.worker_pool.active_count() > 0:
            result = self.worker_pool.wait_any(
                timeout=self.worker_pool.max_concurrent * 60
            )
            if result is not None:
                tid, worker_result = result
                self._on_worker_complete(tid, worker_result)

    def _verify(self, d: dict) -> None:
        """PM 自验：把 task 标 done。

        简化实现：相信 PM 的判断（PM 已读过 worker 反馈和 acceptance_criteria）。
        """
        tid = d.get("task_id")
        task = self.project.tasks.get(tid)
        if task and task.status in ("running", "failed"):
            try:
                # 强制走 done 路径：先 transition running 再 done
                if task.status == "failed":
                    task.transition("pending")
                    task.transition("running")
                task.transition("done")
            except ValueError:
                pass
            self.project.tasks.save()

    def _replan(self, d: dict) -> None:
        """PM 重新拆分：写入新的 TASKS.json。

        注意：已 done 的任务不会被覆盖（TaskStore.replace_all 内部保护）。
        """
        from pm_agent.tasks import Task

        new_tasks_data = (d.get("tasks_update") or {}).get("tasks", [])
        if not new_tasks_data:
            return
        new_tasks = [Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__})
                     for t in new_tasks_data]
        self.project.tasks.replace_all(new_tasks)
        self.project.tasks.save()
        self.project.append_decision_log({"event": "replan_applied", "n_tasks": len(new_tasks)})

    # ---------- worker 完成回调 ----------

    def _on_worker_complete(self, task_id: str, result: "WorkerResult") -> None:
        """Worker 完成后的主线程处理：状态转换 + 持久化。

        注意：file_lock release 和 cost_tracker.add 已在 WorkerPool 的 callback
        里做了（worker 线程，幂等操作）。这里只做主线程独占的非幂等操作。
        """
        # 1. 取消进度监测（unwatch 在主线程做）
        self.progress_monitor.unwatch(task_id)

        # 2. 持久化反馈到队列
        if result.feedback is not None:
            self.project.append_feedback_queue(result.feedback)

        # 4. 更新 task 状态
        task = self.project.tasks.get(task_id)
        if task is None:
            return

        if result.success and result.feedback:
            # 成功路径
            task.last_feedback_summary = result.feedback.get("summary", "")[:500]
            try:
                if task.status == "running":
                    task.transition("done")
            except ValueError:
                pass
            # 应用 memory_updates
            self._apply_memory_updates(result.feedback.get("memory_updates", []))
            # 处理 memory_corrections（双重确认机制）
            self._queue_memory_corrections(
                task_id, result.feedback.get("memory_corrections", [])
            )
        else:
            # 失败路径：attempts++，状态 → failed 或 pending
            task.attempts += 1
            self._failed_cli_history.setdefault(task_id, []).append(task.assigned_cli or "")
            try:
                if task.status == "running":
                    task.transition("failed")
            except ValueError:
                pass
            # 决定是否重派
            if task.attempts < task.max_attempts:
                # 失败重派策略（attempts 1-3）：
                #   1: 原 cli 重派
                #   2: 原 cli 重派（PM 会在下一轮看到 attempts，自己改 prompt）
                #   3: PM 决定换 cli（driver 不主动切，相信 PM 决策）
                try:
                    task.transition("pending")
                except ValueError:
                    pass
            else:
                # 已达最大尝试数——保持 failed，由 PM 在下一轮 escalate
                pass

        self.project.tasks.save()

    def _apply_memory_updates(self, updates: list[dict]) -> None:
        if not updates:
            return
        memory = StructuredMemory.load(self.project.memory_md)
        applied = memory.apply_updates(updates)
        memory.save(self.project.memory_md)
        from pm_agent.utils import append_jsonl
        append_jsonl(self.project.memory_log, {
            "at": iso_now(),
            "applied": applied,
            "total": len(updates),
        })

    def _queue_memory_corrections(self, source_task_id: str, corrections: list[dict]) -> None:
        if not corrections:
            return
        from pm_agent.corrections import MemoryCorrectionStore
        store = MemoryCorrectionStore(self.project)
        for c in corrections:
            store.add(c, source_task_id=source_task_id)

    # ---------- 决策签名 / 卡死检测 ----------

    def _signature(self, decision: Decision) -> tuple:
        d = decision.to_dict()
        action = decision.action
        if action == "dispatch":
            return (action, d.get("task_id", ""), d.get("cli", ""))
        if action == "dispatch_parallel":
            batch = d.get("batch", [])
            items = tuple(sorted((t["task_id"], t["cli"]) for t in batch))
            return (action, items)
        if action in ("verify", "replan"):
            return (action, d.get("task_id"))
        return (action,)

    @staticmethod
    def _is_similar_decision(sig_a: Optional[tuple], sig_b: Optional[tuple]) -> bool:
        if sig_a is None or sig_b is None:
            return False
        if sig_a == sig_b:
            return True
        if sig_a[0] != sig_b[0]:
            return False
        if sig_a[0] == "dispatch":
            # 同任务即视为相似，cli 切换不算进展
            return sig_a[1] == sig_b[1]
        if sig_a[0] == "dispatch_parallel":
            tasks_a = {tid for tid, _ in sig_a[1]}
            tasks_b = {tid for tid, _ in sig_b[1]}
            if not tasks_a or not tasks_b:
                return False
            overlap = len(tasks_a & tasks_b) / max(len(tasks_a), len(tasks_b))
            return overlap >= 0.8
        return False

    # ---------- 工具 ----------

    def _collect_recent_events(self) -> list[dict]:
        """收集 PM 应该感知的近期系统事件。"""
        decisions = self.project.load_recent_decisions(n=20)
        return [
            d for d in decisions
            if d.get("event") in (
                "partial_batch_rejected",
                "worker_killed_by_monitor",
                "guardrails_violation",
                "single_dispatch_rejected",
                "budget_degrade",
            )
        ]

    def _save_pool_state_to_disk(self) -> None:
        status = self.worker_pool.status()
        self.project.save_worker_pool_state(status.get("active", []))

    def _notify(self, msg: str) -> None:
        """终端打印 + 日志。"""
        print(f"[pm-agent] {msg}", flush=True)
        self.project.append_decision_log({"event": "notify", "message": msg})
