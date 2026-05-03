"""PMSessionManager：PM 长 session CLI 生命周期管理（决策 5 状态机）。

定位：传输层（vs PMAgent 业务层）。负责启动/重启 CLI session、发 prompt、
接 response、超时重试、recovery prompt 注入。

状态机：
- 正常：session 活着，decide() 直接发 prompt
- timeout（>320s）：kill 当前 session，进入 blocked_pm_timeout，等 5min
- 重试：启动新 session，注入完整 MEMORY/GUARDRAILS/PROJECT recovery prompt
- 失败：连续 N 次（默认 3）失败，抛 PMSessionMaxRetriesError → PMAgent
        转成 escalate decision

注意：本模块只管"如何调 CLI"，不管"问什么"——prompt 构造由 PMAgent 负责。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pm_agent.project_store import ProjectStore

log = logging.getLogger(__name__)


# 决策 5 参数
PM_DECISION_TIMEOUT_SEC = 320      # 单次决策硬超时
PM_RETRY_INTERVAL_SEC = 300        # 卡死后等多久重试（5 分钟）
PM_MAX_RETRIES = 3                 # 最多重试次数，超过转 escalate


class PMSessionTimeoutError(Exception):
    """PM 单次决策超时。"""


class PMSessionMaxRetriesError(Exception):
    """PM 重试次数超过上限——PMAgent 应转成 escalate decision。"""


class PMSessionManager:
    """PM CLI 长 session 生命周期管理。

    用法（由 PMAgent 持有，不直接被 driver 调）：
        mgr = PMSessionManager(project, ["claude", "--print", ...])
        try:
            decision = mgr.decide(prompt)
        except PMSessionMaxRetriesError:
            return {"action": "escalate", "reason": "pm_max_retries"}
        ...
        mgr.shutdown()
    """

    def __init__(
        self,
        project: "ProjectStore",
        pm_cli_command: list[str],
        decision_timeout_sec: int = PM_DECISION_TIMEOUT_SEC,
        retry_interval_sec: int = PM_RETRY_INTERVAL_SEC,
        max_retries: int = PM_MAX_RETRIES,
    ):
        self.project = project
        self.pm_cli_command = pm_cli_command
        self.decision_timeout_sec = decision_timeout_sec
        self.retry_interval_sec = retry_interval_sec
        self.max_retries = max_retries

        self.session: Optional[subprocess.Popen] = None
        self.retry_count = 0
        # 单线程 executor 用来包装阻塞的 send_recv 调用，可以 timeout 取消等待
        # （但不能真的中断阻塞的 read，所以也要 kill subprocess）
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pm-decide"
        )

    # ---------- session 生命周期 ----------

    def start_session(self, with_memory_recovery: bool = False) -> None:
        """启动新 PM session。

        with_memory_recovery=True：重启后注入完整 MEMORY/GUARDRAILS/PROJECT
        让 PM 恢复上下文。
        """
        log.info(f"启动 PM session, recovery={with_memory_recovery}")

        # 关掉旧的 session（如果还在）
        if self.session is not None and self.session.poll() is None:
            try:
                self.session.kill()
            except Exception:
                pass

        self.session = subprocess.Popen(
            self.pm_cli_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # 行缓冲（关键：让 readline 不死锁）
        )

        if with_memory_recovery:
            recovery_prompt = self._build_recovery_prompt()
            try:
                self.session.stdin.write(recovery_prompt + "\n")
                self.session.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                log.error(f"recovery prompt 写入失败: {e}")
                return
            # 简化：给 CLI 2s 加载（生产可换 expect 等待 "READY"）
            time.sleep(2)

    def _build_recovery_prompt(self) -> str:
        """构造重启时的恢复 prompt。"""
        try:
            from pm_agent.memory import StructuredMemory
            mem = StructuredMemory.load(self.project.memory_md)
            memory_text = mem.to_markdown()
        except Exception as e:
            memory_text = f"(MEMORY load failed: {e})"

        try:
            guardrails_text = self.project.guardrails_md.read_text(encoding="utf-8")
        except OSError:
            guardrails_text = "(GUARDRAILS.md missing)"

        try:
            project_text = self.project.project_md.read_text(encoding="utf-8")
        except OSError:
            project_text = "(PROJECT.md missing)"

        return f"""[SYSTEM RESTART] 你的上一个 session 因决策超时被 driver 重启。
以下是项目当前状态，请基于此继续工作。后续 driver 会发送状态更新让你做新决策。

# PROJECT.md
{project_text}

# GUARDRAILS.md
{guardrails_text}

# MEMORY.md
{memory_text}

请回复 "READY" 表明你已加载完成。
"""

    # ---------- 决策主流程 ----------

    def decide(self, prompt: str) -> dict:
        """发 prompt 给 PM 等返回 decision JSON。

        320s timeout 触发时，启动状态机重试流程。

        Raises:
            PMSessionMaxRetriesError: 重试 N 次仍超时
        """
        if self.session is None or self.session.poll() is not None:
            log.warning("PM session 不活，启动新 session")
            self.start_session(with_memory_recovery=False)

        # 用 executor 包装阻塞调用，可以 timeout
        future = self._executor.submit(self._send_and_recv, prompt)

        try:
            result = future.result(timeout=self.decision_timeout_sec)
            self.retry_count = 0  # 成功一次清零
            return result
        except FuturesTimeoutError:
            log.error(
                f"PM 决策超过 {self.decision_timeout_sec}s，进入重试流程"
            )
            self._handle_timeout()
            return self._retry_after_timeout(prompt)

    def _send_and_recv(self, prompt: str) -> dict:
        """发 prompt，读 response 直到拿到完整 JSON。

        简化实现：从 stdout 一行行读，组合后用 regex 抓最大的 JSON 块。
        生产可用 pexpect 处理 CLI 提示符 / 多轮 ack。
        """
        if self.session is None or self.session.stdin is None:
            raise RuntimeError("session not initialized")

        try:
            self.session.stdin.write(prompt + "\n")
            self.session.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"PM session stdin write failed: {e}")

        # 读 stdout 直到拿到完整 JSON
        output_lines: list[str] = []
        max_lines = 10000  # 防止无限读
        for _ in range(max_lines):
            line = self.session.stdout.readline()
            if not line:
                raise RuntimeError("PM session stdout closed unexpectedly")
            output_lines.append(line)

            # 启发式：累计输出后尝试解析 JSON 块
            joined = "".join(output_lines)
            decision = self._extract_decision_json(joined)
            if decision is not None:
                return decision

        raise RuntimeError(f"PM 输出 {max_lines} 行后仍无合法 decision JSON")

    @staticmethod
    def _extract_decision_json(text: str) -> Optional[dict]:
        """从 PM 输出中抽取 decision JSON。

        策略：找最后一个 {...} 块（PM 可能先讲 reasoning 再输出 JSON）。
        必须含 "action" 字段才认。
        """
        # 用贪婪匹配找最后一个 {...}
        matches = list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL))
        for m in reversed(matches):
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict) and "action" in data:
                    return data
            except json.JSONDecodeError:
                continue
        return None

    # ---------- timeout 状态机 ----------

    def _handle_timeout(self) -> None:
        """timeout 触发，kill 当前 session。"""
        log.warning("kill PM session due to timeout")
        if self.session is not None and self.session.poll() is None:
            try:
                self.session.kill()
                self.session.wait(timeout=10)
            except Exception as e:
                log.error(f"kill PM session 失败: {e}")
        self.session = None

    def _retry_after_timeout(self, original_prompt: str) -> dict:
        """重试流程：等 5min → 重启 session（带 recovery）→ 重发 prompt。

        递归调用：内层 timeout 再触发递归直到 max_retries。
        """
        self.retry_count += 1
        if self.retry_count > self.max_retries:
            log.error(
                f"PM 重试 {self.max_retries} 次仍超时，放弃"
            )
            raise PMSessionMaxRetriesError(
                f"PM 连续 {self.max_retries} 次决策超时"
            )

        log.info(
            f"PM 重试 #{self.retry_count}/{self.max_retries}, "
            f"等待 {self.retry_interval_sec}s"
        )
        time.sleep(self.retry_interval_sec)

        # 重启 session 注入完整 MEMORY
        self.start_session(with_memory_recovery=True)

        # 重新发 prompt
        future = self._executor.submit(self._send_and_recv, original_prompt)
        try:
            result = future.result(timeout=self.decision_timeout_sec)
            self.retry_count = 0  # 成功后清零
            return result
        except FuturesTimeoutError:
            self._handle_timeout()
            return self._retry_after_timeout(original_prompt)  # 递归

    # ---------- 关闭 ----------

    def shutdown(self) -> None:
        """driver 退出时调用。"""
        if self.session is not None and self.session.poll() is None:
            try:
                if self.session.stdin is not None:
                    self.session.stdin.write("/exit\n")
                    self.session.stdin.flush()
                self.session.wait(timeout=10)
            except Exception:
                try:
                    self.session.kill()
                except Exception:
                    pass
        self._executor.shutdown(wait=False)
