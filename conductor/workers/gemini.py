"""Gemini CLI Worker。

CLI 命令：gemini -p --yolo
认证：默认走 Google 账号；use_api_key=True 时改用 WORKER_GEMINI_API_KEY。

注：gemini CLI 通过 stdin 接收 prompt，cwd 决定工作目录（不需 --workdir）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from conductor.workers.base import WorkerDispatcher


class GeminiWorker(WorkerDispatcher):
    cli_name = "gemini"

    def build_command(self, worktree: Path) -> list[str]:
        # -p 表示 prompt-from-stdin；--yolo 跳过所有确认
        return ["gemini", "-p", "--yolo"]

    def get_env(self, use_api_key: bool) -> dict:
        env = os.environ.copy()
        if use_api_key:
            worker_key = os.environ.get("WORKER_GEMINI_API_KEY")
            if worker_key:
                env["GEMINI_API_KEY"] = worker_key
            else:
                env.pop("GEMINI_API_KEY", None)
        else:
            env.pop("GEMINI_API_KEY", None)
        return env

    _TOKEN_RE = re.compile(r"tokens?[:\s]+(\d+)", re.IGNORECASE)

    def estimate_cost(self, stdout: str, stderr: str) -> float:
        if "GEMINI_API_KEY" not in os.environ:
            return 0.0
        match = self._TOKEN_RE.search(stderr or "") or self._TOKEN_RE.search(stdout or "")
        if not match:
            return 0.0
        tokens = int(match.group(1))
        # 粗估：Gemini 2.5 Pro 大约 $0.0035 / 1K input tokens
        return tokens * 0.0000035
