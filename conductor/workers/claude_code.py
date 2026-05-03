"""Claude Code Worker。

CLI 命令：claude --print --output-format json --dangerously-skip-permissions
认证：默认走 OAuth 订阅；use_api_key=True 时改用 WORKER_ANTHROPIC_API_KEY。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from conductor.workers.base import WorkerDispatcher


class ClaudeCodeWorker(WorkerDispatcher):
    cli_name = "claude_code"

    def build_command(self, worktree: Path) -> list[str]:
        return [
            "claude",
            "--print",
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]

    def get_env(self, use_api_key: bool) -> dict:
        env = os.environ.copy()
        if use_api_key:
            # 关键：用 WORKER_* 前缀的 key，避免和 PM 自己的 key 混淆
            worker_key = os.environ.get("WORKER_ANTHROPIC_API_KEY")
            if worker_key:
                env["ANTHROPIC_API_KEY"] = worker_key
            else:
                # 没设 worker key 时退回订阅模式
                env.pop("ANTHROPIC_API_KEY", None)
        else:
            # 清掉 PM 进程的 key，让 CLI 走 OAuth 订阅
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def estimate_cost(self, stdout: str, stderr: str) -> float:
        """Claude Code --output-format json 在末尾输出 usage 信息。

        典型格式（最后一行或倒数第二行）：
            {"type":"result", "total_cost_usd":0.04, "usage":{...}}
        """
        if not stdout:
            return 0.0
        # 倒着扫描行，找第一个能解析为含 total_cost_usd 的 JSON
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and "total_cost_usd" in data:
                    return float(data["total_cost_usd"])
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return 0.0
