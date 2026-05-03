"""driver lifecycle 回归测试。

4 个测试：
9. shutdown 后主进程能在 15s 内退出（BUG 1 验证）
10. _dispatch_one 失败后主循环不卡（BUG 2 验证）
11. kill_all 后 file_lock 不泄漏（BUG 3 验证）
12. 进程组真 kill 子进程（跨平台核心）
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pm_agent.concurrency import WorkerPool
from pm_agent.workers.base import WorkerResult


# ---------- 测试 9：shutdown 不 hang（BUG 1）----------

def test_shutdown_main_exit_under_15s():
    """关键回归：driver 退出不应 hang 30 分钟。"""
    code = f"""
import sys
sys.path.insert(0, {repr(str(Path(__file__).parent.parent))})

import time
from pathlib import Path
from pm_agent.concurrency import WorkerPool
from pm_agent.workers.base import WorkerResult

def long_worker(**kw):
    abort = kw['abort_event']
    # 响应 abort 的长 worker（30 分钟，但 abort 应在 5 秒内 kill）
    if abort.wait(timeout=1800):
        return WorkerResult(success=False, error='aborted', aborted=True)
    return WorkerResult(success=True, feedback={{}})

pool = WorkerPool(max_concurrent=2)
pool.submit(task_id='t1', cli_name='fake', worker_fn=long_worker,
            worktree=Path('/tmp'), prompt='')
pool.submit(task_id='t2', cli_name='fake', worker_fn=long_worker,
            worktree=Path('/tmp'), prompt='')
time.sleep(0.5)

t_start = time.monotonic()
pool.shutdown(wait=True, graceful_timeout_sec=2.0)
duration = time.monotonic() - t_start
print(f'SHUTDOWN_DURATION={{duration:.2f}}')
"""

    t_start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    total = time.monotonic() - t_start

    assert result.returncode == 0, f"stderr={result.stderr}"
    assert total < 15, f"shutdown 总耗时 {total:.2f}s 超过 15s（BUG 1 未修复）"
    assert "SHUTDOWN_DURATION=" in result.stdout


# ---------- 测试 10：_dispatch_one 失败不卡主循环（BUG 2）----------

def test_dispatch_failure_does_not_hang():
    """模拟 file_lock 冲突 → 主循环不应在 wait_any 上卡死。"""
    from pm_agent.concurrency import FileLockArbiter

    project = MagicMock()
    project.path = Path("/tmp")

    arbiter = FileLockArbiter(project)
    # 已经被另一个 task 占用
    arbiter.try_acquire("other_task", ["src/api/**"])

    # 这次 try_acquire 应失败
    assert not arbiter.try_acquire("new_task", ["src/api/users/**"])

    # pool 是空的，wait_any 应立即返回 None
    pool = WorkerPool(max_concurrent=2)
    try:
        t_start = time.monotonic()
        result = pool.wait_any(timeout=1)
        duration = time.monotonic() - t_start
        assert result is None
        assert duration < 0.5, f"空 pool 上 wait_any 应立即返回, 实际 {duration}s"
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 11：kill_all 后 file_lock 不泄漏（BUG 3）----------

def test_kill_all_releases_file_lock():
    """worker 完成的 callback 应释放 file_lock。"""
    file_lock = MagicMock()
    cost_tracker = MagicMock()

    pool = WorkerPool(
        max_concurrent=2,
        file_lock_arbiter=file_lock,
        cost_tracker=cost_tracker,
    )

    def slow_worker(**kw):
        abort = kw["abort_event"]
        if abort.wait(timeout=10):
            return WorkerResult(success=False, error="aborted", aborted=True,
                                cost_estimate=0.05)
        return WorkerResult(success=True, feedback={}, cost_estimate=0.05)

    try:
        pool.submit(task_id="t1", cli_name="fake", worker_fn=slow_worker,
                    worktree=Path("/tmp"), prompt="")
        pool.submit(task_id="t2", cli_name="fake", worker_fn=slow_worker,
                    worktree=Path("/tmp"), prompt="")
        time.sleep(0.3)

        pool.kill_all()
        # 等 callback 触发完
        time.sleep(0.3)

        # 验证 file_lock.release 被调用了 2 次
        assert file_lock.release.call_count >= 2, \
            f"file_lock.release 应被调 ≥2 次, 实际 {file_lock.release.call_count}"
        # cost_tracker.add 也应被调（worker 报告了 cost_estimate=0.05）
        assert cost_tracker.add.call_count >= 2, \
            f"cost_tracker.add 应被调 ≥2 次, 实际 {cost_tracker.add.call_count}"
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 12：进程组真 kill 子进程（跨平台）----------

def test_process_group_kills_grandchildren():
    """ProcessGroupController kill_group 必须杀掉 worker 派生的子孙进程。"""
    from pm_agent.process_group import ProcessGroupController

    if sys.platform == "win32":
        # Windows: cmd 启动一个 ping，ping 是 cmd 的子进程
        cmd = ["cmd", "/c", "ping -n 60 127.0.0.1"]
    else:
        # POSIX: bash spawn sleep 后 wait
        cmd = ["bash", "-c", "sleep 60 & wait"]

    with ProcessGroupController() as pg:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **pg.popen_kwargs(),
        )
        pg.attach(proc)
        time.sleep(0.5)

        # 拿子进程
        try:
            children = psutil.Process(proc.pid).children(recursive=True)
            child_pids = [c.pid for c in children]
        except psutil.NoSuchProcess:
            child_pids = []

        pg.kill_group()
        time.sleep(1)

        assert not psutil.pid_exists(proc.pid), \
            f"父进程 pid {proc.pid} 应已死"
        for cpid in child_pids:
            assert not psutil.pid_exists(cpid), \
                f"子孙进程 pid {cpid} 应已被 kill"
