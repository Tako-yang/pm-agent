"""WorkerDispatcher 抽象基类（v2 - final 模式）。

核心改动 vs v1:
1. dispatch() 是 final（逻辑上不可重写）—— 内部用 ProcessGroupController
   + AbortWatcher 管理子进程生命周期。子类只实现 build_command / get_env /
   parse_output / estimate_cost 4 个方法。
2. dispatch 接受 abort_event 参数（来自 WorkerPool），AbortWatcher 监听该 event
   触发 kill_group，实现外部中断。
3. WorkerResult 增加 pre_dispatch_sha / pid / aborted / duration_sec 字段，
   供 driver 做 worktree 清理 / 错误诊断。
4. 注入 4 个 env 变量给 orphan scanner 识别（PM_AGENT_PROJECT_ID 等）。
5. dispatch 签名改 keyword-only —— 避免 ThreadPoolExecutor.submit 的 kwargs
   与 bound method 的 self 冲突。

子类如果真有"完全自定义流程"的需求（如 HTTP API worker 不走 subprocess），
可重写 _dispatch_low_level()，但要自己处理 abort_event。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pm_agent.abort_watcher import AbortWatcher
from pm_agent.feedback import parse_feedback_block
from pm_agent.process_group import ProcessGroupController

log = logging.getLogger(__name__)


@dataclass
class WorkerResult:
    """Worker 一次 dispatch 的结果。

    success=True 当且仅当：未超时 + 未被 abort + 退出码 0 + FEEDBACK 解析成功
                       + status != failed
    """

    success: bool
    stdout: str = ""
    stderr: str = ""
    feedback: Optional[dict] = None
    cost_estimate: float = 0.0
    error: Optional[str] = None
    duration_sec: float = 0.0
    extra: dict = field(default_factory=dict)
    # v2 新增字段
    pre_dispatch_sha: Optional[str] = None  # 派发前 git SHA，供 worktree 回滚
    pid: Optional[int] = None  # 子进程 pid，供日志 / orphan scan
    aborted: bool = False  # 是否被 abort_event 中断（vs 自然退出/timeout）


class WorkerDispatcher(ABC):
    """所有 Worker CLI 的抽象基类。

    final 模式：子类只覆写 build_command / get_env / parse_output / estimate_cost。
    dispatch() 由基类管理进程生命周期，**逻辑上不可被覆写**。

    高级覆写：如子类需要完全自定义流程（如 HTTP API worker），
    可覆写 _dispatch_low_level()，但要自己处理 abort_event。
    """

    cli_name: str = ""  # 子类必填

    # ---------- 必须实现的抽象方法 ----------

    @abstractmethod
    def build_command(self, worktree: Path) -> list[str]:
        """构造 subprocess 命令。"""

    @abstractmethod
    def get_env(self, use_api_key: bool) -> dict:
        """构造环境变量。决定走 API key 还是订阅认证。"""

    # ---------- 可选重写方法 ----------

    def parse_output(self, stdout: str) -> Optional[dict]:
        """解析 worker 输出。默认抓 <FEEDBACK> 块。"""
        return parse_feedback_block(stdout)

    def estimate_cost(self, stdout: str, stderr: str) -> float:
        """估算本次调用的成本（USD）。子类按 CLI 输出特征实现。"""
        return 0.0

    # ---------- final dispatch（不要重写）----------

    def dispatch(
        self,
        *,
        prompt: str,
        worktree: Path,
        timeout_sec: int = 1800,
        no_output_timeout_sec: int = 600,
        abort_event: Optional[threading.Event] = None,
        use_api_key: bool = False,
        project_id: str = "",
        task_id: str = "",
        **extra_kwargs,
    ) -> WorkerResult:
        """final 模式：不可被子类覆写。委托给 _dispatch_low_level。

        参数 keyword-only —— 避免 ThreadPoolExecutor.submit 的 kwargs
        与 bound method 的位置参数冲突。
        """
        return self._dispatch_low_level(
            prompt=prompt,
            worktree=worktree,
            timeout_sec=timeout_sec,
            no_output_timeout_sec=no_output_timeout_sec,
            abort_event=abort_event or threading.Event(),
            use_api_key=use_api_key,
            project_id=project_id,
            task_id=task_id,
            **extra_kwargs,
        )

    def _dispatch_low_level(
        self,
        *,
        prompt: str,
        worktree: Path,
        timeout_sec: int,
        no_output_timeout_sec: int,
        abort_event: threading.Event,
        use_api_key: bool,
        project_id: str,
        task_id: str,
        **extra_kwargs,
    ) -> WorkerResult:
        """高级覆写逃生口。子类重写需自己处理：
        - ProcessGroupController 包装
        - AbortWatcher 启动
        - timeout / abort 处理
        - WorkerResult 字段填充
        """
        cmd = self.build_command(worktree)
        env = self.get_env(use_api_key)

        # 注入 orphan scanner 识别标记
        env["PM_AGENT_PROJECT_ID"] = project_id
        env["PM_AGENT_TASK_ID"] = task_id
        env["PM_AGENT_DRIVER_PID"] = str(os.getpid())
        env["PM_AGENT_BORN_AT"] = datetime.now(timezone.utc).isoformat()

        # log 路径：<project>/logs/<task_id>.log
        # 用 task_id 而非 worktree.name，避免同 worktree 多次 dispatch 覆盖
        logs_dir = worktree.parent.parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_name = task_id or worktree.name
        log_path = logs_dir / f"{log_name}.log"

        # 派发前 git SHA 快照——失败回滚锚点
        try:
            pre_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pre_sha = None  # worktree 不是 git 仓库（罕见）

        error: Optional[str] = None
        aborted = False
        stdout = ""
        stderr = ""
        pid: Optional[int] = None
        start_time = time.monotonic()
        watcher: Optional[AbortWatcher] = None

        # === 用 ProcessGroupController 包装整个生命周期 ===
        with ProcessGroupController() as pg:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(worktree),
                    env=env,
                    text=True,
                    **pg.popen_kwargs(),
                )
            except FileNotFoundError as e:
                duration = time.monotonic() - start_time
                self._write_log_file(
                    log_path,
                    stdout=f"--- BINARY NOT FOUND: {e} ---",
                    stderr="",
                    duration=duration,
                    error="binary_not_found",
                    aborted=False,
                )
                return WorkerResult(
                    success=False,
                    error=f"binary_not_found: {cmd[0]}",
                    duration_sec=duration,
                )
            except Exception as e:
                duration = time.monotonic() - start_time
                self._write_log_file(
                    log_path,
                    stdout="",
                    stderr=f"--- SPAWN FAILED: {e} ---",
                    duration=duration,
                    error="spawn_failed",
                    aborted=False,
                )
                return WorkerResult(
                    success=False,
                    error=f"spawn_failed: {e}",
                    duration_sec=duration,
                )

            pid = proc.pid
            try:
                pg.attach(proc)  # Windows: 入 Job 然后 resume
            except RuntimeError as e:
                # attach 失败 —— 子进程已被 kill，pg.__exit__ 会清理
                duration = time.monotonic() - start_time
                return WorkerResult(
                    success=False,
                    error=f"attach_failed: {e}",
                    duration_sec=duration,
                    pid=pid,
                )

            # 启动 abort watcher
            watcher = AbortWatcher(proc, pg, abort_event, task_id=task_id)
            watcher.start()

            # 写 prompt 到 stdin 后立即关闭（让 CLI 知道输入结束）
            try:
                if proc.stdin is not None:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
            except (BrokenPipeError, OSError) as e:
                log.debug(f"stdin write failed (proc may have exited): {e}")

            # 等进程完成
            try:
                stdout, stderr = proc.communicate(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                error = "hard_timeout"
                pg.kill_group()
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    stdout = stdout or ""
                    stderr = (stderr or "") + "\n[kill 后未在 10s 内收到输出]"

            watcher.stop()

            # 如果是 abort 触发的退出，标记
            if watcher.triggered:
                aborted = True
                error = "aborted"

        # ProcessGroupController 退出 with 块时已 cleanup（关 Job 句柄）

        duration = time.monotonic() - start_time

        # 等 watcher 线程退出（normally 在 stop() 后立即 return）
        if watcher is not None:
            watcher.join(timeout=1)

        # 解析 FEEDBACK
        feedback = self.parse_output(stdout) if stdout else None

        # 错误码补全（按优先级：abort > timeout > non_zero > no_feedback > feedback_failed）
        if error is None and proc.returncode != 0:
            error = f"non_zero_exit:{proc.returncode}"
        if error is None and feedback is None:
            error = "no_feedback"
        if error is None and feedback and feedback.get("status") == "failed":
            error = "feedback_failed"

        success = (error is None)

        # 写完整日志（含 META）
        self._write_log_file(
            log_path,
            stdout=stdout or "",
            stderr=stderr or "",
            duration=duration,
            error=error,
            aborted=aborted,
        )

        # 持久化 FEEDBACK 便于事后调试
        if feedback is not None:
            feedback_path = logs_dir / f"{log_name}.feedback.json"
            try:
                feedback_path.write_text(
                    json.dumps(feedback, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as e:
                log.warning(f"写 feedback.json 失败: {e}")

        # 失败路径走清理（reset --hard 回滚 + 归档现场）
        if not success and pre_sha:
            self._cleanup_failed_dispatch(
                worktree=worktree,
                pre_dispatch_sha=pre_sha,
                task_id=log_name,
                stderr=stderr or "",
                reason=error or "unknown",
            )

        return WorkerResult(
            success=success,
            stdout=stdout or "",
            stderr=stderr or "",
            feedback=feedback,
            cost_estimate=self.estimate_cost(stdout or "", stderr or ""),
            error=error,
            duration_sec=duration,
            pre_dispatch_sha=pre_sha,
            pid=pid,
            aborted=aborted,
        )

    # ---------- 辅助方法 ----------

    @staticmethod
    def _write_log_file(
        log_path: Path,
        stdout: str,
        stderr: str,
        duration: float,
        error: Optional[str],
        aborted: bool,
    ) -> None:
        try:
            log_path.write_text(
                f"{stdout}\n--- STDERR ---\n{stderr}\n"
                f"--- META ---\nduration_sec={duration:.2f} "
                f"error={error} aborted={aborted}\n",
                encoding="utf-8",
            )
        except OSError as e:
            log.error(f"写 log 失败 {log_path}: {e}")

    def _cleanup_failed_dispatch(
        self,
        worktree: Path,
        pre_dispatch_sha: str,
        task_id: str,
        stderr: str,
        reason: str,
    ) -> None:
        """所有失败分支共用的清理路径：
        归档现场 → reset --hard → clean -fd。
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        logs_dir = worktree.parent.parent / "logs"
        archive_dir = logs_dir / f"{task_id}_{ts}_failed"
        archive_dir.mkdir(parents=True, exist_ok=True)

        # (a) 归档：相对 pre_sha 的 diff
        try:
            with (archive_dir / "git_diff.patch").open("w", encoding="utf-8") as f:
                subprocess.run(
                    ["git", "diff", pre_dispatch_sha],
                    cwd=worktree, stdout=f, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

        try:
            with (archive_dir / "git_log.txt").open("w", encoding="utf-8") as f:
                subprocess.run(
                    ["git", "log", f"{pre_dispatch_sha}..HEAD", "--oneline"],
                    cwd=worktree, stdout=f, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

        try:
            with (archive_dir / "untracked.txt").open("w", encoding="utf-8") as f:
                subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    cwd=worktree, stdout=f, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

        try:
            (archive_dir / "stderr_tail.txt").write_text(
                "\n".join(stderr.splitlines()[-200:]),
                encoding="utf-8",
            )
        except OSError:
            pass

        try:
            (archive_dir / "reason.txt").write_text(reason, encoding="utf-8")
        except OSError:
            pass

        # (b) 回滚已 commit 的改动
        subprocess.run(
            ["git", "reset", "--hard", pre_dispatch_sha],
            cwd=worktree, capture_output=True,
        )
        # (c) 清理 untracked 文件
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=worktree, capture_output=True,
        )
