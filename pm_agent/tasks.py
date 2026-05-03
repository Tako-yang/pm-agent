"""TASKS.json 管理：状态机 + 依赖检查 + 文件锁字段。

任务状态机：
    pending → running → (done | failed | blocked)
    failed → pending（attempts++，可重派）
    blocked → pending（依赖完成时由 driver 自动推动）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jsonschema import validate as _jsonschema_validate

from pm_agent.utils import iso_now, load_json_schema

VALID_STATUSES = {"pending", "running", "done", "failed", "blocked"}
TASKS_SCHEMA = load_json_schema("tasks.schema.json")


@dataclass
class Task:
    id: str
    title: str
    status: str = "pending"
    description: str = ""
    assigned_cli: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)
    acceptance_criteria: str = ""
    files_owned: list[str] = field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 3
    last_feedback_summary: str = ""
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)

    def transition(self, new_status: str) -> None:
        """状态机校验后切换。非法转换抛 ValueError。"""
        if new_status not in VALID_STATUSES:
            raise ValueError(f"非法状态: {new_status}")

        # 状态转换矩阵
        allowed = {
            "pending": {"running", "blocked"},
            "running": {"done", "failed", "blocked"},
            "done": set(),  # 终态
            "failed": {"pending"},  # 重派
            "blocked": {"pending"},  # 解除阻塞
        }
        if new_status not in allowed.get(self.status, set()):
            raise ValueError(f"非法状态转换: {self.status} → {new_status}")

        self.status = new_status
        self.updated_at = iso_now()
        if new_status == "pending" and self.status == "failed":
            self.attempts += 1


class TaskStore:
    """TASKS.json 的读写封装。

    所有写操作都立即落盘——状态文件化原则的硬约束。
    """

    def __init__(self, path: Path, project_id: str):
        self.path = path
        self.project_id = project_id
        self._tasks: dict[str, Task] = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        _jsonschema_validate(data, TASKS_SCHEMA)
        for t in data.get("tasks", []):
            task = Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__})
            self._tasks[task.id] = task

    def save(self) -> None:
        data = {
            "project_id": self.project_id,
            "version": 1,
            "tasks": [asdict(t) for t in self._tasks.values()],
        }
        _jsonschema_validate(data, TASKS_SCHEMA)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ---------- 查询 ----------

    def all(self) -> list[Task]:
        return list(self._tasks.values())

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def by_status(self, status: str) -> list[Task]:
        return [t for t in self._tasks.values() if t.status == status]

    def runnable(self) -> list[Task]:
        """返回所有 pending 且依赖已满足的任务。"""
        done_ids = {t.id for t in self.by_status("done")}
        return [
            t for t in self.by_status("pending")
            if all(dep in done_ids for dep in t.depends_on)
        ]

    def all_done(self) -> bool:
        if not self._tasks:
            return False
        return all(t.status in ("done",) for t in self._tasks.values())

    def has_failed_exhausted(self) -> list[Task]:
        """返回 attempts >= max_attempts 的失败任务（需升级 Boss）。"""
        return [
            t for t in self._tasks.values()
            if t.status == "failed" and t.attempts >= t.max_attempts
        ]

    def progress_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in VALID_STATUSES}
        for t in self._tasks.values():
            counts[t.status] += 1
        counts["total"] = sum(counts.values())
        return counts

    # ---------- 修改 ----------

    def upsert(self, task: Task) -> None:
        if task.id in self._tasks:
            existing = self._tasks[task.id]
            for fname in Task.__dataclass_fields__:
                if fname in ("created_at",):
                    continue
                setattr(existing, fname, getattr(task, fname))
            existing.updated_at = iso_now()
        else:
            self._tasks[task.id] = task

    def replace_all(self, tasks: list[Task]) -> None:
        """replan 时全量替换。保留已 done 的任务避免回归。"""
        kept_done = {tid: t for tid, t in self._tasks.items() if t.status == "done"}
        self._tasks = {**kept_done}
        for t in tasks:
            if t.id in kept_done:
                continue  # 不覆盖已 done
            self._tasks[t.id] = t

    def transition(self, task_id: str, new_status: str) -> None:
        task = self._tasks[task_id]
        task.transition(new_status)
        self.save()

    def apply_feedback(self, task_id: str, feedback: dict) -> None:
        """根据 worker FEEDBACK 更新任务状态和摘要。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.last_feedback_summary = feedback.get("summary", "")[:500]
        status = feedback.get("status", "failed")
        if status == "completed":
            if task.status == "running":
                task.transition("done")
        else:
            if task.status == "running":
                task.attempts += 1
                if task.attempts >= task.max_attempts:
                    # 仍标记 failed，由 driver/PM 决定是 escalate
                    task.transition("failed")
                else:
                    # 回到 pending 等待重派
                    task.transition("failed")
                    task.transition("pending")
        self.save()
