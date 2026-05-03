"""成本追踪与预算控制（详见 PRD F7 / TDD §4.1）。

三道护栏：
1. 单轮 PM token 上限（≤ 8K input + 4K output，由 PM_MAX_TOKENS 控制）
2. 单任务 token 预算（默认 50K，driver 通过 timeout_sec 间接限制）
3. Project 总预算（用户在 init 时设定，超 80% 告警，100% 强制 kill）

并发自动降级：
- 70% 时 Driver 调 worker_pool.set_max_concurrent(1)
- 80% 时 Driver 创建 budget_80 escalation
- 100% 时 Driver 调 worker_pool.kill_all + escalate
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pm_agent.utils import iso_now

if TYPE_CHECKING:
    from rich.console import Console


@dataclass
class CostEntry:
    at: str
    source: str  # "pm:iter_5" / "worker:task_007"
    amount: float  # USD
    meta: dict = field(default_factory=dict)


class CostTracker:
    """成本追踪。落盘到 .pm/cost.json，每次 add 立即 flush。"""

    DEFAULT_BUDGET = 50.0

    def __init__(self, project_path: Path, budget: Optional[float] = None):
        self.project_path = Path(project_path)
        self.cost_file = self.project_path / ".pm" / "cost.json"
        self.cost_file.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[CostEntry] = []
        self._budget = budget or self.DEFAULT_BUDGET
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not self.cost_file.exists():
            return
        try:
            data = json.loads(self.cost_file.read_text(encoding="utf-8"))
            self._budget = data.get("budget", self._budget)
            self._entries = [CostEntry(**e) for e in data.get("entries", [])]
        except (json.JSONDecodeError, OSError):
            pass

    def _save(self) -> None:
        data = {
            "budget": self._budget,
            "total": self.total(),
            "entries": [e.__dict__ for e in self._entries],
            "updated_at": iso_now(),
        }
        self.cost_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ---------- API ----------

    @property
    def budget(self) -> float:
        return self._budget

    def set_budget(self, budget: float) -> None:
        self._budget = float(budget)
        self._save()

    def add(self, source: str, amount: float, meta: Optional[dict] = None) -> None:
        if amount <= 0:
            return
        self._entries.append(CostEntry(
            at=iso_now(),
            source=source,
            amount=float(amount),
            meta=meta or {},
        ))
        self._save()

    def total(self) -> float:
        return sum(e.amount for e in self._entries)

    def ratio(self) -> float:
        if self._budget <= 0:
            return 0.0
        return self.total() / self._budget

    def exceeded(self) -> bool:
        return self.total() >= self._budget

    def warning(self) -> bool:
        return self.ratio() >= 0.8

    def should_degrade(self) -> bool:
        return self.ratio() >= 0.7

    # ---------- 摘要 ----------

    def summary(self) -> dict:
        by_source: dict[str, float] = {}
        for e in self._entries:
            prefix = e.source.split(":", 1)[0]
            by_source[prefix] = by_source.get(prefix, 0.0) + e.amount
        return {
            "total": self.total(),
            "budget": self._budget,
            "ratio": self.ratio(),
            "by_source": by_source,
            "n_entries": len(self._entries),
        }

    def print_summary(self, console: "Console") -> None:
        s = self.summary()
        console.print(f"[bold]累计成本[/bold]: ${s['total']:.4f} / ${s['budget']:.2f} ({s['ratio']:.1%})")
        console.print(f"[bold]条目数[/bold]: {s['n_entries']}")
        console.print("[bold]按来源[/bold]:")
        for src, amt in sorted(s["by_source"].items(), key=lambda x: -x[1]):
            console.print(f"  {src}: ${amt:.4f}")
        if s["ratio"] >= 1.0:
            console.print("[red]✗ 已超预算[/red]")
        elif s["ratio"] >= 0.8:
            console.print("[yellow]⚠ 已达 80%[/yellow]")
        elif s["ratio"] >= 0.7:
            console.print("[yellow]⚠ 已达 70%（并发降级）[/yellow]")
