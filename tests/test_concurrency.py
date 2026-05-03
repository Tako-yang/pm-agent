"""WorkerPool / AbortWatcher / kill_all 三段式 / status phase 等回归测试。

8 个测试覆盖：
1. submit + wait_any 闭环
2. first-completed 语义
3. PoolFullError
4. abort_event 真 kill 真 subprocess（BUG 3）
5. kill_all 三段式
6. worker 异常 → failed result（不 raise）
7. status phase 字段
8. 多 worker 同时完成都能取到
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pm_agent.concurrency import (
    DuplicateTaskError,
    PoolFullError,
    WorkerPool,
)
from pm_agent.workers.base import WorkerResult


# ---------- helper：fake worker_fn ----------

def _quick_ok(**kw):
    """0.1 秒后返回成功。"""
    time.sleep(0.1)
    return WorkerResult(success=True, stdout="ok", feedback={"task_id": "t"}, cost_estimate=0.0)


def _slow_worker(duration: float):
    """返回一个跑 duration 秒的 worker_fn。"""
    def fn(**kw):
        abort = kw.get("abort_event")
        if abort and abort.wait(timeout=duration):
            return WorkerResult(success=False, error="aborted", aborted=True)
        return WorkerResult(success=True, stdout=f"slept {duration}", feedback={}, cost_estimate=0.0)
    return fn


# ---------- 测试 1：submit + wait_any 完整闭环 ----------

def test_submit_runs_and_wait_any_returns():
    pool = WorkerPool(max_concurrent=3)
    try:
        pool.submit(
            task_id="t1", cli_name="fake", worker_fn=_quick_ok,
            worktree=Path("/tmp"), prompt="hi",
        )
        result = pool.wait_any(timeout=5)
        assert result is not None
        task_id, r = result
        assert task_id == "t1"
        assert r.success
        assert pool.active_count() == 0
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 2：first-completed 语义 ----------

def test_first_completed_wins():
    pool = WorkerPool(max_concurrent=3)
    try:
        pool.submit(task_id="slow", cli_name="fake", worker_fn=_slow_worker(2.0),
                    worktree=Path("/tmp"), prompt="")
        pool.submit(task_id="medium", cli_name="fake", worker_fn=_slow_worker(1.0),
                    worktree=Path("/tmp"), prompt="")
        pool.submit(task_id="fast", cli_name="fake", worker_fn=_slow_worker(0.2),
                    worktree=Path("/tmp"), prompt="")

        task_id, _ = pool.wait_any(timeout=5)
        assert task_id == "fast"

        # 后续 wait_any 应返回剩下的（顺序 medium → slow）
        next_task, _ = pool.wait_any(timeout=5)
        assert next_task == "medium"

        last_task, _ = pool.wait_any(timeout=5)
        assert last_task == "slow"
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 3：PoolFullError ----------

def test_pool_full_raises():
    pool = WorkerPool(max_concurrent=2)
    try:
        pool.submit(task_id="t1", cli_name="fake", worker_fn=_slow_worker(10),
                    worktree=Path("/tmp"), prompt="")
        pool.submit(task_id="t2", cli_name="fake", worker_fn=_slow_worker(10),
                    worktree=Path("/tmp"), prompt="")

        with pytest.raises(PoolFullError):
            pool.submit(task_id="t3", cli_name="fake", worker_fn=_slow_worker(10),
                        worktree=Path("/tmp"), prompt="")
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 4：abort_event 真 kill 真 subprocess（BUG 3 验证）----------

def test_abort_event_kills_real_subprocess():
    """启动真的 sleep subprocess，abort 它，验证进程 1 秒内被 kill。"""
    from pm_agent.process_group import ProcessGroupController
    from pm_agent.abort_watcher import AbortWatcher

    pool = WorkerPool(max_concurrent=1)

    def real_subprocess_worker(**kw):
        abort_event = kw["abort_event"]
        with ProcessGroupController() as pg:
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                **pg.popen_kwargs(),
            )
            pg.attach(proc)

            watcher = AbortWatcher(proc, pg, abort_event, "test")
            watcher.start()

            try:
                proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                pg.kill_group()
                proc.communicate(timeout=5)
            watcher.stop()
            watcher.join(timeout=1)

            return WorkerResult(
                success=False if watcher.triggered else True,
                error="aborted" if watcher.triggered else None,
                aborted=watcher.triggered,
                pid=proc.pid,
            )

    try:
        pool.submit(task_id="t1", cli_name="fake", worker_fn=real_subprocess_worker,
                    worktree=Path("/tmp"), prompt="")

        time.sleep(0.5)  # 等 worker 启动 subprocess

        t_kill = time.monotonic()
        killed = pool.kill_one("t1", graceful_timeout_sec=3.0)
        duration = time.monotonic() - t_kill

        assert killed, "kill_one 应在 3 秒内完成"
        assert duration < 3.0

        result = pool.wait_any(timeout=2)
        assert result is not None
        task_id, r = result
        assert r.aborted, "aborted 应为 True"
        assert r.pid is not None
        assert not psutil.pid_exists(r.pid), f"subprocess pid {r.pid} 应已死"
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 5：kill_all 三段式清理 ----------

def test_kill_all_three_stage_cleanup():
    pool = WorkerPool(max_concurrent=3)

    def long_worker(**kw):
        abort = kw["abort_event"]
        # 模拟响应 abort_event 的 worker（10 秒内会被 abort）
        for _ in range(100):
            if abort.wait(timeout=0.1):
                return WorkerResult(success=False, error="aborted", aborted=True)
        return WorkerResult(success=True, feedback={})

    try:
        for i in range(3):
            pool.submit(task_id=f"t{i}", cli_name="fake", worker_fn=long_worker,
                        worktree=Path("/tmp"), prompt="")

        time.sleep(0.5)

        t_start = time.monotonic()
        killed = pool.kill_all(graceful_timeout_sec=5, force_timeout_sec=5)
        duration = time.monotonic() - t_start

        assert len(killed) == 3, f"应 kill 3 个，实际 {killed}"
        assert duration < 6.0, f"应 < 6 秒，实际 {duration:.2f}s"
        assert pool.active_count() == 0
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 6：worker 异常 → failed result ----------

def test_worker_exception_returns_failed_result():
    pool = WorkerPool(max_concurrent=1)

    def crashing_worker(**kw):
        raise RuntimeError("故意崩溃")

    try:
        pool.submit(task_id="t1", cli_name="fake", worker_fn=crashing_worker,
                    worktree=Path("/tmp"), prompt="")

        result = pool.wait_any(timeout=3)
        assert result is not None
        _, r = result
        assert r.success is False
        assert "worker_exception" in (r.error or "")
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 7：status phase 字段（决策 3 验证） ----------

def test_status_includes_phase_field():
    pool = WorkerPool(max_concurrent=2)

    try:
        pool.submit(task_id="quick", cli_name="fake", worker_fn=_quick_ok,
                    worktree=Path("/tmp"), prompt="")
        pool.submit(task_id="slow", cli_name="fake", worker_fn=_slow_worker(5),
                    worktree=Path("/tmp"), prompt="")

        # 等 quick 完成，slow 还在跑
        time.sleep(0.5)

        status = pool.status()
        phases = {t["task_id"]: t["phase"] for t in status["active"]}

        # quick 已 done 但还在 active（主线程没 wait_any），应是 completed_pending_handle
        assert phases.get("quick") == "completed_pending_handle"
        assert phases.get("slow") == "running"
    finally:
        pool.shutdown(wait=False)


# ---------- 测试 8：多 worker 同时完成都能取到 ----------

def test_simultaneous_completion_all_retrievable():
    pool = WorkerPool(max_concurrent=3)

    barrier = threading.Barrier(3)

    def synced_worker(**kw):
        barrier.wait()  # 3 个 worker 在 barrier 处对齐，几乎同时返回
        return WorkerResult(success=True, feedback={})

    try:
        for i in range(3):
            pool.submit(task_id=f"t{i}", cli_name="fake", worker_fn=synced_worker,
                        worktree=Path("/tmp"), prompt="")

        seen = set()
        for _ in range(3):
            result = pool.wait_any(timeout=5)
            assert result is not None
            seen.add(result[0])

        assert seen == {"t0", "t1", "t2"}
    finally:
        pool.shutdown(wait=False)
