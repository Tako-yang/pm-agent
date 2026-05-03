"""MemoryCorrectionStore：双重确认机制（防 worker 幻觉污染 MEMORY）。

机制（详见 PRD F4.4）：
- worker A 报告"已知坑里那条 Tailwind 4 @apply 不准确" → memory_corrections
- PM 不立即应用：把它存入 pending_corrections.json，标"待验证"
- 下一个独立 worker 任务的 prompt 中要求验证
- worker B 也确认 → take_confirmed → 真正应用到 MEMORY
- worker B 反对 → 记录但仍 pending（再下一个 worker 验证）

简化策略：MVP 阶段需要 1 个独立 worker 同意即应用（而非 2 个）——
原 TDD 写法 `>= 1` 已经是这个意思，保持一致。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from conductor.utils import iso_now

if TYPE_CHECKING:
    from conductor.project_store import ProjectStore


class MemoryCorrectionStore:
    """worker 提出的 memory_corrections 不立即应用，
    需要另一个独立 worker 验证后才生效。"""

    def __init__(self, project: "ProjectStore"):
        self.project = project
        self.path = project.pending_corrections

    # ---------- 内部 ----------

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(items, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ---------- API ----------

    def add(self, correction: dict, source_task_id: str) -> str:
        """添加一条待验证的修正。返回新条目的 id。"""
        items = self._load()
        cid = uuid.uuid4().hex[:12]
        items.append({
            "id": cid,
            "correction": correction,
            "source_task_id": source_task_id,
            "verifications": [],
            "resolved": False,
            "created_at": iso_now(),
        })
        self._save(items)
        return cid

    def get_pending_for_injection(self) -> list[dict]:
        """返回需要下一个 worker 验证的项（尚无验证记录）。"""
        return [
            p for p in self._load()
            if not p.get("resolved") and len(p.get("verifications", [])) < 1
        ]

    def add_verification(
        self,
        correction_id: str,
        verifying_task_id: str,
        agreed: bool,
        reason: str = "",
    ) -> None:
        items = self._load()
        for p in items:
            if p["id"] == correction_id:
                p["verifications"].append({
                    "task_id": verifying_task_id,
                    "agreed": agreed,
                    "reason": reason,
                    "at": iso_now(),
                })
        self._save(items)

    def take_confirmed(self) -> list[dict]:
        """取出已被独立 worker 同意的修正——这些会被应用到 MEMORY。"""
        items = self._load()
        confirmed = []
        remaining = []
        for p in items:
            if p.get("resolved"):
                remaining.append(p)
                continue
            agreed_count = sum(
                1 for v in p.get("verifications", []) if v.get("agreed")
            )
            if agreed_count >= 1:
                p["resolved"] = True
                p["resolved_at"] = iso_now()
                confirmed.append(p)
                remaining.append(p)  # 保留作为审计痕迹
            else:
                remaining.append(p)
        self._save(remaining)
        return confirmed

    def apply_confirmed_to_memory(self) -> int:
        """driver 周期性调用：把已确认的修正应用到 MEMORY.md。

        返回应用的条数。
        """
        confirmed = self.take_confirmed()
        if not confirmed:
            return 0
        from conductor.memory import StructuredMemory

        memory = StructuredMemory.load(self.project.memory_md)
        n = 0
        for p in confirmed:
            try:
                memory.apply_update(p["correction"])
                n += 1
            except Exception:
                pass
        if n > 0:
            memory.save(self.project.memory_md)
        return n
