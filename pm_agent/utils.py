"""通用工具：时间戳、JSON Schema 加载、JSONL 追加等。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any


def iso_now() -> str:
    """UTC ISO8601 时间戳（带 Z 后缀，无微秒，便于排序）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json_schema(name: str) -> dict:
    """从 pm_agent.schemas 包内加载 JSON Schema。"""
    with resources.files("pm_agent.schemas").joinpath(name).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_prompt(name: str) -> str:
    """从 pm_agent.prompts 包内加载 prompt 模板。"""
    return resources.files("pm_agent.prompts").joinpath(name).read_text(encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """以追加模式写一行 JSON 到 .jsonl 文件。线程不安全，调用方负责加锁。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")


def read_jsonl(path: Path, n: int | None = None) -> list[dict]:
    """读取 .jsonl 的最后 n 行（None 表示全部）。

    简单实现：全文读取后切片。MVP 阶段日志预期不会过大。
    """
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    if n is not None:
        lines = lines[-n:]
    return [json.loads(line) for line in lines]


def read_tail(path: Path, n_bytes: int = 8192) -> str:
    """读文件末尾 n 字节，处理 UTF-8 边界。"""
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > n_bytes:
            f.seek(size - n_bytes)
            f.read(1)  # 跳过可能的半字符
        return f.read().decode("utf-8", errors="replace")
