"""并发模块（v2）：WorkerPool / FileLockArbiter / WorkerProgressMonitor。

v2 vs v1 关键改动：
- WorkerPool 用 abort_event 机制（不是 future.cancel —— 它对已启动 future 无效）
- WorkerPool 接受 file_lock_arbiter + cost_tracker 注入，callback 自动调
- wait_any 返回 (task_id, result) 元组（不是 future 集合）
- status() 加 phase 字段，区分 running / completed_pending_handle
- kill_all 三段式（决策 6）：abort_event → 等 5s → 再等 5s → zombie 标记
- shutdown 顺序：kill_all → wait → executor.shutdown（修 BUG 1 主进程 hang）

线程模型：
- ThreadPoolExecutor 跑 worker（阻塞在 subprocess.communicate）
- 主线程通过 wait_any 等任意完成
- callback 在 worker 线程跑，只做幂等纯状态操作（file_lock / cost）
- 状态转换 / memory_updates 由 driver 主线程在 _on_worker_complete 做
"""
from __future__ import annotations

import concurrent.futures
import fnmatch
import logging
import re
import threading
import time
from concurrent.futures import (
    ALL_COMPLETED, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from conductor.workers.base import WorkerResult

if TYPE_CHECKING:
    from conductor.cost import CostTracker
    from conductor.project_store import ProjectStore

log = logging.getLogger(__name__)


# =====================================================================
# WorkerPool
# =====================================================================

class PoolFullError(RuntimeError):
    """WorkerPool 已满，无法接受新任务。"""


class DuplicateTaskError(RuntimeError):
    """同一 task_id 重复 submit。"""


@dataclass
class ActiveWorker:
    """跟踪一个正在跑的 worker。"""

    task_id: str
    cli_name: str
    future: Future
    submit_time: float
    worktree: Path
    timeout_sec: int = 1800
    no_output_timeout_sec: int = 600
    abort_event: threading.Event = field(default_factory=threading.Event)


class WorkerPool:
    """并发 Worker 管理（v2）。

    构造时可注入 file_lock_arbiter / cost_tracker，callback 在 worker 完成时
    自动调它们的 release / add。这两个操作是幂等的，放在 worker 线程不影响
    线程安全；其余状态转换由 driver 主线程通过 wait_any 拿到结果后处理。
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        file_lock_arbiter: Optional["FileLockArbiter"] = None,
        cost_tracker: Optional["CostTracker"] = None,
    ):
        self.max_concurrent = max_concurrent
        self.file_lock_arbiter = file_lock_arbiter
        self.cost_tracker = cost_tracker
        self.executor = ThreadPoolExecutor(
            max_workers=max(max_concurrent * 2, 4),
            thread_name_prefix="conductor-worker",
        )
        self.active: dict[str, ActiveWorker] = {}
        self._lock = threading.Lock()

    # ---------- 提交 ----------

    def submit(
        self,
        *,
        task_id: str,
        cli_name: str,
        worker_fn: Callable[..., WorkerResult],
        worktree: Path,
        timeout_sec: int = 1800,
        no_output_timeout_sec: int = 600,
        **dispatch_kwargs: Any,
    ) -> Future:
        """提交一个 worker 到线程池。

        worker_fn 必须接受 keyword-only：
            worker_fn(*, prompt, worktree, timeout_sec, no_output_timeout_sec,
                      abort_event, **kwargs) -> WorkerResult

        所有 dispatch_kwargs（含 prompt / project_id 等）会作为 keyword 传给 worker_fn。
        """
        with self._lock:
            if len(self.active) >= self.max_concurrent:
                raise PoolFullError(
                    f"Pool 已满（{len(self.active)}/{self.max_concurrent}）"
                )
            if task_id in self.active:
                raise DuplicateTaskError(f"task_id {task_id} 已在运行中")

            abort_event = threading.Event()

            future = self.executor.submit(
                worker_fn,
                worktree=worktree,
                timeout_sec=timeout_sec,
                no_output_timeout_sec=no_output_timeout_sec,
                abort_event=abort_event,
                task_id=task_id,
                **dispatch_kwargs,
            )

            self.active[task_id] = ActiveWorker(
                task_id=task_id,
                cli_name=cli_name,
                future=future,
                submit_time=time.monotonic(),
                worktree=worktree,
                timeout_sec=timeout_sec,
                no_output_timeout_sec=no_output_timeout_sec,
                abort_event=abort_event,
            )
            log.info(f"submitted task={task_id} cli={cli_name}")

            future.add_done_callback(
                lambda f, tid=task_id: self._handle_done_callback(tid, f)
            )
            return future

    # ---------- callback（worker 线程）只做幂等纯状态 ----------

    def _handle_done_callback(self, task_id: str, future: Future) -> None:
        """worker 线程完成时调，只做幂等操作（决策 2）：
        - file_lock release（幂等 pop）
        - cost_tracker.add（accumulator）

        **不**在这里 pop self.active —— 主线程 wait_any 拿到结果后才 pop，
        让 driver 能在 status() 看到 'completed_pending_handle' 状态。

        **不**在这里做 status 转换 / memory_updates / project.save —— 那些
        是非幂等的，必须在 driver 主线程串行做（避免 race condition）。
        """
        # 拿 result（必须 wrap，因为 future.result() 会 raise worker 抛的异常）
        result: Optional[WorkerResult] = None
        try:
            if future.cancelled():
                pass  # cancelled 没 result，下游 wait_any 会构造 failed
            else:
                result = future.result()
        except Exception as e:
            log.error(f"task={task_id} worker 抛异常: {e}")

        # 1. file_lock release（幂等：pop None 不报错）
        if self.file_lock_arbiter is not None:
            try:
                self.file_lock_arbiter.release(task_id)
            except Exception as e:
                log.error(f"file_lock release 失败 task={task_id}: {e}")

        # 2. cost_tracker.add
        if self.cost_tracker is not None and result is not None and result.cost_estimate > 0:
            try:
                self.cost_tracker.add(
                    source=f"worker:{task_id}",
                    amount=result.cost_estimate,
                    meta={"duration_sec": result.duration_sec},
                )
            except Exception as e:
                log.error(f"cost_tracker add 失败 task={task_id}: {e}")

    # ---------- 等待（主线程用） ----------

    def wait_any(
        self,
        timeout: Optional[float] = None,
    ) -> Optional[tuple[str, WorkerResult]]:
        """阻塞等任意一个 worker 完成，返回 (task_id, result)。

        超时 / pool 空 → None。

        完成后**从 active pop**，让出槽位给下一次 submit。
        """
        with self._lock:
            if not self.active:
                return None
            futures_snapshot = {w.future: w.task_id for w in self.active.values()}

        done, _ = wait(
            list(futures_snapshot.keys()),
            timeout=timeout,
            return_when=FIRST_COMPLETED,
        )

        if not done:
            return None  # 超时

        # done 可能多个，本次只取一个；其他下次 wait_any 立即返回（已 done）
        future = next(iter(done))
        task_id = futures_snapshot[future]

        result = self._extract_result(task_id, future)

        # 关键：pop active 让出槽位
        with self._lock:
            self.active.pop(task_id, None)

        return task_id, result

    def wait_all(
        self,
        timeout: Optional[float] = None,
    ) -> list[tuple[str, WorkerResult]]:
        """阻塞等所有 worker 完成，返回所有结果。

        deadline 超时即停止，剩余的留在 active 里，下次 wait_any 再处理。
        """
        results: list[tuple[str, WorkerResult]] = []
        deadline = time.monotonic() + timeout if timeout is not None else None

        while True:
            with self._lock:
                if not self.active:
                    break

            remaining = (deadline - time.monotonic()) if deadline is not None else None
            if remaining is not None and remaining <= 0:
                break

            r = self.wait_any(timeout=remaining)
            if r is None:
                break
            results.append(r)

        return results

    @staticmethod
    def _extract_result(task_id: str, future: Future) -> WorkerResult:
        """从 future 拿 result，异常 / cancel 转成 failed WorkerResult。"""
        try:
            if future.cancelled():
                return WorkerResult(success=False, error="cancelled")
            return future.result()
        except concurrent.futures.CancelledError:
            return WorkerResult(success=False, error="cancelled")
        except Exception as e:
            log.error(f"task={task_id} worker exception: {e}")
            return WorkerResult(
                success=False,
                error=f"worker_exception: {e}",
                stderr=str(e),
            )

    # ---------- 状态查询（PM / driver 用） ----------

    def available_slots(self) -> int:
        with self._lock:
            return max(0, self.max_concurrent - len(self.active))

    def active_count(self) -> int:
        """driver 在 wait_any 前调用避免空 pool 上等。"""
        with self._lock:
            return len(self.active)

    def status(self) -> dict:
        """返回 pool 状态快照，被 driver 注入到 PM prompt。

        决策 3：phase 字段标识 running / completed_pending_handle。
        future.done()=True 但仍在 active 的标 pending_handle，让 PM 知道
        driver 即将处理它，避免基于过时信息决策。
        """
        with self._lock:
            now = time.monotonic()
            return {
                "max_concurrent": self.max_concurrent,
                "active_count": len(self.active),
                "available_slots": self.max_concurrent - len(self.active),
                # 兼容旧 driver 调用：status['active'] 也保留
                "active": [
                    {
                        "task_id": w.task_id,
                        "cli_name": w.cli_name,
                        "elapsed_sec": int(now - w.submit_time),
                        "phase": "completed_pending_handle" if w.future.done() else "running",
                    }
                    for w in self.active.values()
                ],
            }

    # ---------- Kill ----------

    def kill_one(
        self,
        task_id: str,
        graceful_timeout_sec: float = 5.0,
    ) -> bool:
        """Kill 单个 worker。

        流程：
        1. set abort_event（AbortWatcher 在 dispatch 内 0.1s 检测到 → kill_group）
        2. 等 future 完成（worker 线程从 communicate 返回）最多 graceful_timeout_sec

        返回：是否在超时内完成 kill。
        """
        with self._lock:
            worker = self.active.get(task_id)

        if worker is None:
            log.warning(f"kill_one: task={task_id} not in active")
            return False

        worker.abort_event.set()
        log.info(f"kill_one: abort_event set for task={task_id}")

        try:
            worker.future.result(timeout=graceful_timeout_sec)
            return True
        except concurrent.futures.TimeoutError:
            log.error(
                f"kill_one: task={task_id} 在 {graceful_timeout_sec}s 内未退出"
            )
            return False
        except Exception:
            # worker 抛异常也算 kill 成功（进程已退）
            return True

    def kill_all(
        self,
        graceful_timeout_sec: float = 5.0,
        force_timeout_sec: float = 5.0,
    ) -> list[str]:
        """Kill 所有 active workers，三段式清理（决策 6）。

        阶段 1：全部 set abort_event，等 graceful_timeout_sec
        阶段 2：仍未退出的，再等 force_timeout_sec（abort_event 已触发，应已在杀）
        阶段 3：还在的标 zombie，记 error 日志，**不**从 active 移除
                （让下次 wait_any 拿到，要么完成要么报 cancelled）

        返回：成功 kill 的 task_id 列表。
        """
        with self._lock:
            tasks = list(self.active.items())

        if not tasks:
            return []

        log.warning(f"kill_all: 准备 kill {len(tasks)} 个 active workers")

        # 阶段 1: 全部 set abort_event
        for _, worker in tasks:
            worker.abort_event.set()

        futures = [w.future for _, w in tasks]
        done1, pending1 = wait(
            futures, timeout=graceful_timeout_sec, return_when=ALL_COMPLETED
        )

        successfully_killed: list[str] = [
            tid for tid, w in tasks if w.future in done1
        ]

        # 阶段 2
        if pending1:
            log.warning(
                f"kill_all 阶段 1 超时, {len(pending1)} workers 仍在跑, "
                f"继续等 {force_timeout_sec}s"
            )
            done2, pending2 = wait(
                list(pending1), timeout=force_timeout_sec, return_when=ALL_COMPLETED,
            )
            successfully_killed.extend(
                tid for tid, w in tasks if w.future in done2
            )

            # 阶段 3
            if pending2:
                zombie_tids = [tid for tid, w in tasks if w.future in pending2]
                log.error(
                    f"kill_all 阶段 2 仍超时, {len(pending2)} workers 进入 zombie 状态. "
                    f"对应 task: {zombie_tids}. "
                    f"这些 worker 可能仍占用 pool 槽位，需要 driver 重启 + "
                    f"orphan_scanner 才能完全清理."
                )

        # 把成功 kill 的从 active 移除（zombie 不动，等 driver 重启清）
        with self._lock:
            for tid in successfully_killed:
                self.active.pop(tid, None)

        return successfully_killed

    # ---------- 动态调整并发 ----------

    def set_max_concurrent(self, n: int) -> None:
        """动态调整并发上限（用于预算降级）。"""
        with self._lock:
            self.max_concurrent = max(1, n)

    # ---------- 关闭 ----------

    def shutdown(
        self,
        wait: bool = True,
        graceful_timeout_sec: float = 60.0,
    ) -> None:
        """安全关闭 WorkerPool（修 BUG 1：主进程 hang）。

        流程：
        1. wait=True 时优雅等 graceful_timeout_sec（让 worker 自己跑完）
        2. 仍有的 kill_all（三段式：5s + 5s = 10s 内必退）
        3. 最后等 5s 让线程从 communicate 返回
        4. executor.shutdown(wait=True) 收线程（此时所有线程已退）

        典型最坏耗时：graceful + 10 + 5 = 75s（用户传 60s graceful 时）
        实际场景：worker 几秒内被 abort kill 完成，shutdown 总耗时 < 15s。
        """
        if wait:
            log.info(f"shutdown: 优雅等待最多 {graceful_timeout_sec}s")
            self.wait_all(timeout=graceful_timeout_sec)

        with self._lock:
            remaining = len(self.active)
        if remaining > 0:
            log.warning(f"shutdown: 仍有 {remaining} 个 worker，强 kill")
            self.kill_all(graceful_timeout_sec=5.0, force_timeout_sec=5.0)

        # 此时所有 subprocess 应已退出，worker 线程会从 communicate 返回
        with self._lock:
            futures = [w.future for w in self.active.values()]
        if futures:
            from concurrent.futures import wait as cf_wait
            cf_wait(futures, timeout=5.0, return_when=ALL_COMPLETED)

        # 收 executor（线程都退了，瞬间完成）
        self.executor.shutdown(wait=True)
        log.info("shutdown: WorkerPool closed")


# =====================================================================
# FileLockArbiter（v1 保留，无改动）
# =====================================================================

PROTECTED_FILES = [
    "PROJECT.md",
    "GUARDRAILS.md",
    "MEMORY.md",
    "TASKS.json",
    ".pm/**",
    ".pm/*",
    "MEMORY.history/**",
]


def _normalize_glob(p: str) -> str:
    """统一 glob：去前导 ./，反斜杠转正斜杠。"""
    p = p.replace("\\", "/").lstrip("./")
    return p


def _glob_match(filename: str, pattern: str) -> bool:
    """fnmatch 不支持 ** 语义，自己处理一下。"""
    filename = _normalize_glob(filename)
    pattern = _normalize_glob(pattern)
    if "**" in pattern:
        regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return bool(re.fullmatch(regex, filename))
    return fnmatch.fnmatch(filename, pattern)


def globs_overlap(pattern_a: str, pattern_b: str) -> bool:
    """判断两个 glob pattern 是否可能匹配到同一文件。"""
    a = _normalize_glob(pattern_a)
    b = _normalize_glob(pattern_b)

    if a == b:
        return True

    def prefix(p: str) -> str:
        if "**" in p:
            return p.split("**", 1)[0].rstrip("/")
        for i, ch in enumerate(p):
            if ch in "*?[":
                return p[:i].rstrip("/")
        return p

    pa, pb = prefix(a), prefix(b)
    if pa == pb:
        return True
    if pa.startswith(pb + "/") or pb.startswith(pa + "/"):
        return True
    if _glob_match(a, b) or _glob_match(b, a):
        return True
    return False


def globs_overlap_any(globs_a: list[str], globs_b: list[str]) -> bool:
    return any(globs_overlap(a, b) for a in globs_a for b in globs_b)


class FileLockArbiter:
    """并发任务的 files_owned 冲突检测。"""

    def __init__(self, project: "ProjectStore"):
        self.project = project
        self._held: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def try_acquire(self, task_id: str, files_owned: list[str]) -> bool:
        with self._lock:
            if task_id in self._held:
                return True  # 幂等

            for pattern in files_owned:
                for protected in PROTECTED_FILES:
                    if globs_overlap(pattern, protected):
                        return False

            for held_task, held_globs in self._held.items():
                if globs_overlap_any(files_owned, held_globs):
                    return False

            self._held[task_id] = list(files_owned)
            return True

    def release(self, task_id: str) -> None:
        with self._lock:
            self._held.pop(task_id, None)

    def held_state(self) -> dict[str, list[str]]:
        with self._lock:
            return {k: list(v) for k, v in self._held.items()}


# =====================================================================
# WorkerProgressMonitor（v1 保留，仅适配 kill_one）
# =====================================================================

@dataclass
class _WatchState:
    task_id: str
    log_path: Path
    acceptance_check: str
    last_size: int = 0
    last_growth_at: float = field(default_factory=time.time)
    kill_votes: int = 0
    last_reason: str = ""


class WorkerProgressMonitor:
    """Worker 运行期进度监测——卡死 / 偏离方向时通过 abort_event 触发 kill。"""

    CHECK_INTERVAL_SEC = 60
    NO_OUTPUT_TIMEOUT_SEC = 300
    ERROR_FLOOD_THRESHOLD = 20
    DRIFT_PHRASES = [
        "i don't understand",
        "cannot find",
        "task seems unrelated",
        "no clear path forward",
        "无法理解任务",
        "找不到相关代码",
    ]

    def __init__(
        self,
        project: "ProjectStore",
        worker_pool: WorkerPool,
        check_interval: Optional[int] = None,
    ):
        self.project = project
        self.worker_pool = worker_pool
        self.check_interval = check_interval or self.CHECK_INTERVAL_SEC
        self._watching: dict[str, _WatchState] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def watch(self, task_id: str, acceptance_check: str = "") -> None:
        log_path = self.project.path / "logs" / f"{task_id}.log"
        with self._lock:
            self._watching[task_id] = _WatchState(
                task_id=task_id,
                log_path=log_path,
                acceptance_check=acceptance_check,
            )

    def unwatch(self, task_id: str) -> None:
        with self._lock:
            self._watching.pop(task_id, None)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self.check_interval)
            if self._stop_event.is_set():
                break
            with self._lock:
                items = list(self._watching.items())
            for task_id, state in items:
                try:
                    verdict = self._evaluate(state)
                except Exception:
                    continue
                if verdict == "kill":
                    self._kill_and_replan(task_id, state.last_reason)

    def _evaluate(self, state: _WatchState) -> str:
        if not state.log_path.exists():
            return "ok"
        size = state.log_path.stat().st_size
        now = time.time()

        if size == state.last_size:
            if now - state.last_growth_at > self.NO_OUTPUT_TIMEOUT_SEC:
                state.last_reason = "no_output_timeout"
                return "kill"
        else:
            state.last_size = size
            state.last_growth_at = now

        tail = self._read_tail(state.log_path, n_lines=200)
        error_lines = sum(1 for line in tail if " error " in line.lower())
        drift_hit = any(
            phrase in line.lower()
            for line in tail
            for phrase in self.DRIFT_PHRASES
        )
        if error_lines >= self.ERROR_FLOOD_THRESHOLD:
            state.kill_votes += 1
            state.last_reason = f"error_flood({error_lines})"
        elif drift_hit:
            state.kill_votes += 1
            state.last_reason = "self_reported_drift"
        else:
            state.kill_votes = 0

        return "kill" if state.kill_votes >= 2 else "ok"

    @staticmethod
    def _read_tail(path: Path, n_lines: int = 200) -> list[str]:
        try:
            size = path.stat().st_size
            n_bytes = min(size, n_lines * 200)
            with path.open("rb") as f:
                f.seek(size - n_bytes)
                if size > n_bytes:
                    f.read(1)
                data = f.read().decode("utf-8", errors="replace")
            return data.splitlines()[-n_lines:]
        except OSError:
            return []

    def _kill_and_replan(self, task_id: str, reason: str) -> None:
        """通过 worker_pool.kill_one 用 abort_event 真正杀掉。"""
        self.worker_pool.kill_one(task_id, graceful_timeout_sec=5.0)
        self.unwatch(task_id)
        self.project.append_decision_log({
            "event": "worker_killed_by_monitor",
            "task_id": task_id,
            "reason": reason,
            "guidance_for_pm": (
                "此任务被进度监测器中止。请重新评估：是任务描述不清？"
                "是 acceptance_criteria 不可达？是否换一个 cli 类型？"
                "或拆得更细？请输出新的 dispatch 决策。"
            ),
        })
