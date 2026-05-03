"""跨平台进程组管理 v2 —— Job Object (Windows) / setsid (POSIX)。

设计原则：
1. 用平台原生内核原语（Windows Job Object + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
   POSIX setsid + killpg(SIGKILL)）做"硬保证"层。Worker 派生的子孙进程整组
   在内核层面被一次性 kill，不依赖用户态枚举。
2. psutil 作为"辅助层"：kill 后扫残（_scan_residue）、driver 启动时清理孤儿
   （cleanup_stale_workers）。理论上 kernel 已清干净，psutil 是双保险。
3. AbstractContextManager 模式 —— 异常路径自动 cleanup（释放 Job 句柄）。
4. Windows: CREATE_SUSPENDED + 入 Job 后再 resume —— 关掉
   "CreateProcess 返回到 AssignProcessToJobObject 之间子进程逃逸" 的 race window。

API:
    with ProcessGroupController() as pg:
        proc = subprocess.Popen(cmd, **pg.popen_kwargs())
        pg.attach(proc)            # Windows: 入 Job 然后 resume
        ...
        pg.kill_group()            # 显式 kill 整组（也可不调，__exit__ 会调）
"""
from __future__ import annotations

import json
import logging
import os
import sys
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Windows 实现
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import win32api
    import win32con
    import win32job

    # subprocess 模块不暴露 CREATE_SUSPENDED——这是 Win32 API 常量。
    # 值见 MSDN: CreateProcess - dwCreationFlags
    _CREATE_SUSPENDED = 0x00000004

    class ProcessGroupController(AbstractContextManager):
        """Windows: Job Object + KILL_ON_JOB_CLOSE。

        保证语义：Job 句柄关闭时，kernel 原子终止 Job 内全部进程。
        即使 Python 进程崩溃，OS 也会随句柄释放清理。
        """

        def __init__(self):
            self.job = None
            self.proc: Optional[subprocess.Popen] = None
            self._closed = False

        def __enter__(self):
            self.job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            # 只设 KILL_ON_JOB_CLOSE。不设 BREAKAWAY_OK——禁止子进程 detach 逃出 Job
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, info
            )
            return self

        @staticmethod
        def popen_kwargs() -> dict:
            """Popen 必传的 creationflags：
            - CREATE_BREAKAWAY_FROM_JOB：防止 pm-agent 自身已经在某个 Job 里
              （IDE / Docker / Service Manager）时，子进程被继承到那个 Job 上
              而无法加入我们自己的 Job。
            - CREATE_SUSPENDED：进程创建后立即暂停，等 attach 完成再 resume，
              消除"主线程在加入 Job 之前 fork 出新进程"的 race window。
            - CREATE_NEW_PROCESS_GROUP：让 Ctrl+Break 信号路由可控。
            """
            return {
                "creationflags": (
                    subprocess.CREATE_BREAKAWAY_FROM_JOB
                    | _CREATE_SUSPENDED
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            }

        def attach(self, proc: subprocess.Popen) -> None:
            """加入 Job 后 resume。失败时 kill 子进程并 raise。"""
            self.proc = proc
            handle = None
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                    False,
                    proc.pid,
                )
                win32job.AssignProcessToJobObject(self.job, handle)
            except Exception as e:
                # attach 失败：兜底 kill 子进程
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError(
                    f"AssignProcessToJobObject failed for pid={proc.pid}: {e}"
                ) from e
            finally:
                if handle is not None:
                    try:
                        win32api.CloseHandle(handle)
                    except Exception:
                        pass

            # 入 Job 成功后 resume 主线程（这里依赖 psutil 拿 thread；
            # 若 psutil 不可用降级到 ResumeThread via win32process）
            try:
                import psutil
                psutil.Process(proc.pid).resume()
            except ImportError:
                # 退回到 win32process.ResumeThread（需要 thread handle，复杂）
                # 此场景实际很少——psutil 是默认依赖。这里只是兜底。
                log.warning("psutil unavailable, child stays suspended")
            except Exception as e:
                # 进程已死 / 权限问题
                log.warning(f"resume process {proc.pid} failed: {e}")

        def kill_group(self) -> None:
            """原子 kill 整个 Job —— 内核保证组内全员立即死亡。"""
            if self.job is None:
                return
            try:
                win32job.TerminateJobObject(self.job, 1)
            except Exception as e:
                log.warning(f"TerminateJobObject failed: {e}")
            self._scan_residue()

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self._closed:
                return None
            self.kill_group()
            if self.job is not None:
                try:
                    win32api.CloseHandle(self.job)
                except Exception:
                    pass
                self.job = None
            self._closed = True
            return None

        def _scan_residue(self) -> None:
            """psutil 验残：理论应为空，有就强杀并告警。

            双保险：覆盖 KILL_ON_JOB_CLOSE 万一异常的极端场景
            （如 Job 句柄已被泄漏到其它进程持有等罕见情况）。
            """
            if self.proc is None:
                return
            try:
                import psutil
            except ImportError:
                return
            try:
                p = psutil.Process(self.proc.pid)
                survivors = p.children(recursive=True) + [p]
                if survivors:
                    log.warning(
                        f"Job kill 后仍有 {len(survivors)} 个残留进程, 强杀"
                    )
                for s in survivors:
                    try:
                        s.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 进程已死 = 正常情况
                pass

# ---------------------------------------------------------------------------
# POSIX 实现
# ---------------------------------------------------------------------------

else:
    import signal

    class ProcessGroupController(AbstractContextManager):
        """POSIX: setsid + killpg(SIGKILL)。

        子进程通过 start_new_session=True 自成新 session leader，
        getpgid 返回它的 pgid，killpg 一次性灭整组。
        """

        def __init__(self):
            self.proc: Optional[subprocess.Popen] = None
            self.pgid: Optional[int] = None
            self._closed = False

        def __enter__(self):
            return self

        @staticmethod
        def popen_kwargs() -> dict:
            return {"start_new_session": True}  # 等价于 preexec_fn=os.setsid

        def attach(self, proc: subprocess.Popen) -> None:
            self.proc = proc
            try:
                self.pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                self.pgid = proc.pid  # 进程已死，fallback

        def kill_group(self) -> None:
            if self.pgid is not None:
                try:
                    os.killpg(self.pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self._scan_residue()

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self._closed:
                return None
            self.kill_group()
            self._closed = True
            return None

        def _scan_residue(self) -> None:
            if self.proc is None:
                return
            try:
                import psutil
            except ImportError:
                return
            try:
                p = psutil.Process(self.proc.pid)
                survivors = p.children(recursive=True) + [p]
                if survivors:
                    log.warning(
                        f"killpg 后仍有 {len(survivors)} 个残留进程, 强杀"
                    )
                for s in survivors:
                    try:
                        s.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


# ---------------------------------------------------------------------------
# 启动时残留孤儿清理（driver 启动时调用）
# ---------------------------------------------------------------------------

def cleanup_stale_workers(project_path: Path) -> int:
    """driver 启动时调用：扫描 worker_pool.json 里记录的旧 pid，
    若进程仍存活且命令行 / env 匹配 worker CLI 特征，则杀掉。

    应对场景：上次 driver 被 kill -9 暴力杀掉，连 ProcessGroupController.__exit__
    都没跑（极罕见）。返回清理掉的进程数。

    注意：此函数依赖 psutil，没装则跳过（不致命）。
    """
    pool_state = project_path / ".pm" / "worker_pool.json"
    if not pool_state.exists():
        return 0

    try:
        import psutil
    except ImportError:
        return 0

    try:
        state = json.loads(pool_state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    killed = 0
    for entry in state.get("active", []):
        pid = entry.get("pid")
        cmdline_marker = entry.get("cli_name", "")
        if not pid:
            continue
        try:
            p = psutil.Process(pid)
            cmdline_str = " ".join(p.cmdline())
            # 校验：cmdline 含 worker CLI 名，避免误杀同 pid 的无关进程
            if cmdline_marker and cmdline_marker in cmdline_str:
                # 递归杀子孙——双保险
                for child in p.children(recursive=True):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                p.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # 重置状态文件
    pool_state.write_text(
        json.dumps({"active": [], "cleaned_at": "stale"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return killed
