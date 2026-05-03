"""Codex CLI Worker。

CLI 命令：codex exec --cd <worktree> --full-auto
认证：默认走 OpenAI Plus/Pro 登录；use_api_key=True 时改用 WORKER_OPENAI_API_KEY。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from pm_agent.workers.base import WorkerDispatcher


class CodexWorker(WorkerDispatcher):
    cli_name = "codex"

    def build_command(self, worktree: Path) -> list[str]:
        return ["codex", "exec", "--cd", str(worktree), "--full-auto"]

    def get_env(self, use_api_key: bool) -> dict:
        env = os.environ.copy()
        if use_api_key:
            worker_key = os.environ.get("WORKER_OPENAI_API_KEY")
            if worker_key:
                env["OPENAI_API_KEY"] = worker_key
            else:
                env.pop("OPENAI_API_KEY", None)
        else:
            env.pop("OPENAI_API_KEY", None)
        return env

    # codex CLI 目前不在 stdout 暴露稳定的 cost 字段；保守估算用 stderr
    # 中可能出现的 "tokens used: <n>" 结构。无则返回 0（订阅模式不计费）。
    _TOKEN_RE = re.compile(r"tokens?\s*used[:\s]+(\d+)", re.IGNORECASE)

    def estimate_cost(self, stdout: str, stderr: str) -> float:
        # 订阅模式下成本=0；API 模式下用 token 数 * 估价
        if "OPENAI_API_KEY" not in os.environ:
            return 0.0
        match = self._TOKEN_RE.search(stderr or "") or self._TOKEN_RE.search(stdout or "")
        if not match:
            return 0.0
        tokens = int(match.group(1))
        # 极粗的近似：$0.005 / 1K tokens（GPT-5 mid-tier 估价）
        return tokens * 0.000005
