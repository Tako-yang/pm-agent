"""FEEDBACK 块解析：从 worker stdout 中抓 <FEEDBACK>...</FEEDBACK> JSON 块。

设计要点：
- 容错优先：worker LLM 输出可能在 JSON 前后夹带闲聊或 markdown 代码栅。
- 严格校验：解析出来后用 jsonschema 校验，失败则返回 None 让上层走"无 FEEDBACK"
  的失败路径。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from jsonschema import ValidationError, validate as _validate

from conductor.utils import load_json_schema

FEEDBACK_SCHEMA = load_json_schema("feedback.schema.json")

_FEEDBACK_RE = re.compile(r"<FEEDBACK>\s*(.*?)\s*</FEEDBACK>", re.DOTALL)
# 兼容 worker 在 JSON 外面套 markdown 代码栅（```json ... ```）
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def parse_feedback_block(stdout: str) -> Optional[dict]:
    """从 worker stdout 中提取 FEEDBACK JSON 块。

    返回:
        dict: 解析并通过 schema 校验
        None: 找不到块、JSON 语法错、schema 校验失败 —— 都视为"无效反馈"
    """
    if not stdout:
        return None

    # 优先抓最后一个 FEEDBACK 块（worker 可能多次输出，最后一次为最终结果）
    matches = list(_FEEDBACK_RE.finditer(stdout))
    if not matches:
        return None

    raw = matches[-1].group(1).strip()

    # 剥掉可能的代码栅
    fenced = _CODE_FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    try:
        _validate(data, FEEDBACK_SCHEMA)
    except ValidationError:
        return None

    # 补全可选字段的默认值，避免下游 KeyError
    data.setdefault("memory_updates", [])
    data.setdefault("memory_corrections", [])
    data.setdefault("blockers", [])
    data.setdefault("files_changed", [])
    data.setdefault("lessons_learned", [])
    data.setdefault("key_decisions", [])
    return data
