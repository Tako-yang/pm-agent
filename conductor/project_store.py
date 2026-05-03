"""ProjectStore：每个 project 的文件系统抽象层。

把 PROJECT.md / GUARDRAILS.md / MEMORY.md / TASKS.json / .pm/* 的读写都收敛在这里。
其它模块（driver / pm / worker_pool）通过 ProjectStore 操作磁盘——便于测试 mock，
也便于未来切换存储后端（远程对象存储等）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from conductor.tasks import Task, TaskStore
from conductor.utils import append_jsonl, iso_now, read_jsonl


class ProjectStore:
    DIRS = ["worktrees", "logs", "escalations", "MEMORY.history", ".pm"]

    def __init__(self, path: Path):
        self.path = Path(path)
        self.project_id = self.path.name
        self._tasks: Optional[TaskStore] = None
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        for d in self.DIRS:
            (self.path / d).mkdir(parents=True, exist_ok=True)

    # ---------- 文件路径快捷 ----------

    @property
    def project_md(self) -> Path:
        return self.path / "PROJECT.md"

    @property
    def guardrails_md(self) -> Path:
        return self.path / "GUARDRAILS.md"

    @property
    def memory_md(self) -> Path:
        return self.path / "MEMORY.md"

    @property
    def tasks_json(self) -> Path:
        return self.path / "TASKS.json"

    @property
    def state_json(self) -> Path:
        return self.path / ".pm" / "state.json"

    @property
    def cost_json(self) -> Path:
        return self.path / ".pm" / "cost.json"

    @property
    def decisions_log(self) -> Path:
        return self.path / ".pm" / "decisions.log"

    @property
    def memory_log(self) -> Path:
        return self.path / ".pm" / "memory.log"

    @property
    def feedback_queue(self) -> Path:
        return self.path / ".pm" / "feedback_queue.jsonl"

    @property
    def pending_corrections(self) -> Path:
        return self.path / ".pm" / "pending_corrections.json"

    @property
    def worker_pool_json(self) -> Path:
        return self.path / ".pm" / "worker_pool.json"

    # ---------- Tasks ----------

    @property
    def tasks(self) -> TaskStore:
        if self._tasks is None:
            self._tasks = TaskStore(self.tasks_json, self.project_id)
        return self._tasks

    def reload_tasks(self) -> None:
        self._tasks = TaskStore(self.tasks_json, self.project_id)

    # ---------- 状态 ----------

    def load_state(self) -> dict:
        if not self.state_json.exists():
            return {"phase": "init", "iter": 0, "started_at": iso_now()}
        return json.loads(self.state_json.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        state.setdefault("started_at", iso_now())
        state["updated_at"] = iso_now()
        self.state_json.parent.mkdir(parents=True, exist_ok=True)
        self.state_json.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def set_state(self, phase: str) -> None:
        s = self.load_state()
        s["phase"] = phase
        self.save_state(s)

    # ---------- 决策日志 ----------

    def append_decision_log(self, record: dict) -> None:
        record.setdefault("at", iso_now())
        append_jsonl(self.decisions_log, record)

    def load_recent_decisions(self, n: int = 10) -> list[dict]:
        return read_jsonl(self.decisions_log, n=n)

    # ---------- Worker 反馈队列 ----------

    def append_feedback_queue(self, feedback: dict) -> None:
        record = {"at": iso_now(), "feedback": feedback}
        append_jsonl(self.feedback_queue, record)

    def load_recent_feedback(self, n: int = 5) -> list[dict]:
        return read_jsonl(self.feedback_queue, n=n)

    # ---------- 写 PROJECT.md / GUARDRAILS.md ----------

    def write_project_md(self, content: str) -> None:
        self.project_md.write_text(content, encoding="utf-8")

    def write_guardrails_md(self, content: str) -> None:
        self.guardrails_md.write_text(content, encoding="utf-8")

    def load_project_md(self) -> str:
        if not self.project_md.exists():
            return ""
        return self.project_md.read_text(encoding="utf-8")

    # ---------- 失败任务摘要 ----------

    def needs_human(self) -> bool:
        """检查是否有未回复的 escalation。"""
        from conductor.escalation import EscalationStore

        return len(EscalationStore(self.path).list_pending()) > 0

    def is_done(self) -> bool:
        return self.tasks.all_done()

    # ---------- worker pool 状态 ----------

    def save_worker_pool_state(self, active: list[dict]) -> None:
        self.worker_pool_json.write_text(
            json.dumps({"active": active, "at": iso_now()}, ensure_ascii=False),
            encoding="utf-8",
        )

    def load_worker_pool_state(self) -> dict:
        if not self.worker_pool_json.exists():
            return {"active": []}
        return json.loads(self.worker_pool_json.read_text(encoding="utf-8"))
