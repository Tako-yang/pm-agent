"""AbortWatcher：监听 abort_event，触发时调 ProcessGroupController.kill_group。

设计要点（决策 1）：
- 检查间隔 0.1s（不是 1s）—— event.wait(timeout=0.1) 在 set 时立即唤醒，
  CPU 占用 ~0.1%，响应延迟 ≤ 100ms。
- daemon 线程 —— 主进程退出时 OS 自动回收。
- 进程已退出时立即停止监测，不浪费 CPU 空转。

用法：
    watcher = AbortWatcher(proc, pg, abort_event, task_id="task_007")
    watcher.start()
    try:
        stdout, stderr = proc.communicate(timeout=...)
    finally:
        watcher.stop()  # worker 自然退出时调
    if watcher.triggered:
        # 这次完成是被 abort_event kill 触发的，不是自然结束
        ...
"""
from __future__ import annotations

import logging
import subprocess
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pm_agent.process_group import ProcessGroupController

log = logging.getLogger(__name__)

# 决策 1：0.1s 检查间隔
# - CPU 占用 ~0.1%（每秒 10 次 wakeup，单次 < 100us）
# - abort 响应延迟 ≤ 100ms（event.wait 在 set 时立即返回）
ABORT_CHECK_INTERVAL_SEC = 0.1


class AbortWatcher(threading.Thread):
    """监听 abort_event，触发时立即 kill_group。

    一次性使用：abort_event 触发或进程退出后，watcher 自然终止。
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        pg: "ProcessGroupController",
        abort_event: threading.Event,
        task_id: str = "unknown",
    ):
        super().__init__(daemon=True, name=f"abort-watcher-{task_id}")
        self.proc = proc
        self.pg = pg
        self.abort_event = abort_event
        self.task_id = task_id
        self._stop_event = threading.Event()
        self.triggered = False  # 是否真触发过 kill（供 dispatch 判断退出原因）

    def run(self) -> None:
        """监听循环。

        三种退出条件（任一触发即结束）：
        1. _stop 被 set —— 主线程调 stop()，worker 自然退出
        2. proc.poll() 返回非 None —— 进程已自然退出
        3. abort_event 触发 —— 主动 kill_group
        """
        while not self._stop_event.is_set():
            # 进程已自然退出 → 退出监测
            if self.proc.poll() is not None:
                return

            # 关键：event.wait(timeout) 在 event set 时立即返回 True，
            # 否则到 timeout 返回 False，继续 loop。
            # 不用 time.sleep(0.1) 因为 sleep 不响应 set。
            if self.abort_event.wait(timeout=ABORT_CHECK_INTERVAL_SEC):
                log.info(
                    f"abort_event triggered for task={self.task_id}, "
                    f"killing process group (pid={self.proc.pid})"
                )
                self.triggered = True
                try:
                    self.pg.kill_group()
                except Exception as e:
                    log.error(
                        f"kill_group failed for task={self.task_id}: {e}"
                    )
                return

    def stop(self) -> None:
        """worker 自然退出时主线程调，优雅终止 watcher 线程。"""
        self._stop_event.set()
