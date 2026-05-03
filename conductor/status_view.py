"""CLI 状态可视化（rich）。

实现：
- conductor status <project_id>
- conductor list
- conductor logs / decisions / inspect
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

from conductor.cost import CostTracker
from conductor.escalation import EscalationStore
from conductor.memory import StructuredMemory
from conductor.project_store import ProjectStore
from conductor.utils import read_jsonl, read_tail


def render_status(project_path: Path, console: Console) -> None:
    if not project_path.exists():
        console.print(f"[red]项目不存在: {project_path}[/red]")
        return

    project = ProjectStore(project_path)
    state = project.load_state()
    counts = project.tasks.progress_counts()

    # 头部
    console.print(Panel.fit(
        f"[bold]Project:[/bold] {project.project_id}\n"
        f"[bold]Phase:[/bold] {state.get('phase', '?')}\n"
        f"[bold]Iter:[/bold] {state.get('iter', 0)}\n"
        f"[bold]Started:[/bold] {state.get('started_at', '?')}",
        title="项目状态",
    ))

    # 任务进度
    table = Table(title="任务进度", show_lines=False)
    table.add_column("状态")
    table.add_column("数量", justify="right")
    for status in ["pending", "running", "done", "failed", "blocked"]:
        table.add_row(status, str(counts.get(status, 0)))
    table.add_row("[bold]total[/bold]", f"[bold]{counts.get('total', 0)}[/bold]")
    console.print(table)

    # 成本进度条
    cost = CostTracker(project_path)
    summary = cost.summary()
    progress = Progress(
        TextColumn("[bold]成本"),
        BarColumn(bar_width=40),
        TextColumn("${task.completed:.2f} / ${task.total:.2f}"),
        console=console,
    )
    task_id = progress.add_task("cost", total=max(summary["budget"], 0.01), completed=summary["total"])
    with progress:
        pass  # 一次性渲染
    color = "red" if summary["ratio"] >= 0.8 else "yellow" if summary["ratio"] >= 0.7 else "green"
    console.print(
        f"[{color}]成本:[/{color}] ${summary['total']:.2f} / ${summary['budget']:.2f} ({summary['ratio']:.1%})"
    )

    # 当前 worker pool
    pool = project.load_worker_pool_state()
    if pool.get("active"):
        pool_table = Table(title="运行中 Worker", show_lines=False)
        pool_table.add_column("task_id")
        pool_table.add_column("cli")
        pool_table.add_column("已运行(s)", justify="right")
        for w in pool["active"]:
            pool_table.add_row(
                w.get("task_id", "?"),
                w.get("cli_name", "?"),
                str(w.get("elapsed_sec", 0)),
            )
        console.print(pool_table)
    else:
        console.print("[dim]无运行中 Worker[/dim]")

    # MEMORY 字数
    if project.memory_md.exists():
        mem = StructuredMemory.load(project.memory_md)
        size = mem.total_chars()
        ratio = size / mem.MAX_CHARS
        memcolor = "red" if ratio >= 1.0 else "yellow" if ratio >= 0.8 else "green"
        console.print(
            f"[{memcolor}]MEMORY:[/{memcolor}] {size} / {mem.MAX_CHARS} 字 ({ratio:.0%})"
        )

    # 最近 escalations
    pending = EscalationStore(project_path).list_pending()
    if pending:
        console.print(f"\n[red]{len(pending)} 条未回复 escalation:[/red]")
        for e in pending[-3:]:
            console.print(f"  - [{e['id']}] {e['title']}")


def list_projects(projects_root: Path, console: Console) -> None:
    if not projects_root.exists():
        console.print("[yellow]还没有任何项目[/yellow]")
        return

    table = Table(title="所有项目")
    table.add_column("project_id")
    table.add_column("phase")
    table.add_column("tasks(done/total)", justify="right")
    table.add_column("cost", justify="right")

    for d in sorted(projects_root.iterdir()):
        if not d.is_dir():
            continue
        try:
            project = ProjectStore(d)
            state = project.load_state()
            counts = project.tasks.progress_counts()
            cost = CostTracker(d)
            table.add_row(
                d.name,
                state.get("phase", "?"),
                f"{counts.get('done', 0)}/{counts.get('total', 0)}",
                f"${cost.total():.2f}",
            )
        except Exception:
            table.add_row(d.name, "[red]error[/red]", "-", "-")

    console.print(table)


def show_logs(
    project_path: Path,
    tail: int = 50,
    task: Optional[str] = None,
    console: Optional[Console] = None,
) -> None:
    console = console or Console()
    logs_dir = project_path / "logs"
    if task:
        log_file = logs_dir / f"{task}.log"
        if not log_file.exists():
            console.print(f"[red]无 log: {log_file}[/red]")
            return
        # tail 行
        text = read_tail(log_file, n_bytes=max(tail * 200, 4096))
        lines = text.splitlines()[-tail:]
        console.print("\n".join(lines))
    else:
        # 列出所有 log 文件
        if not logs_dir.exists():
            console.print("[yellow]无日志[/yellow]")
            return
        for f in sorted(logs_dir.iterdir()):
            if f.is_file() and f.name.endswith(".log"):
                console.print(f"  {f.name}  ({f.stat().st_size} bytes)")


def show_decisions(project_path: Path, tail: int = 20, console: Optional[Console] = None) -> None:
    console = console or Console()
    project = ProjectStore(project_path)
    decisions = read_jsonl(project.decisions_log, n=tail)
    for d in decisions:
        at = d.get("at", "?")
        event = d.get("event", "decision")
        if event == "decision":
            decision = d.get("decision", {})
            console.print(f"[dim]{at}[/dim] [bold]{decision.get('action', '?')}[/bold] "
                          f"{decision.get('task_id', '')} {decision.get('reasoning', '')[:80]}")
        else:
            console.print(f"[dim]{at}[/dim] [yellow]{event}[/yellow] {json.dumps(d, ensure_ascii=False)[:200]}")


def inspect_task(
    project_path: Path,
    task_id: str,
    console: Optional[Console] = None,
) -> None:
    console = console or Console()
    project = ProjectStore(project_path)
    task = project.tasks.get(task_id)
    if task is None:
        console.print(f"[red]task {task_id} 不存在[/red]")
        return
    from dataclasses import asdict

    console.print(Panel(json.dumps(asdict(task), indent=2, ensure_ascii=False), title=f"Task {task_id}"))

    # 反馈
    feedback_file = project_path / "logs" / f"{task_id}.feedback.json"
    if feedback_file.exists():
        console.print(Panel(feedback_file.read_text(encoding="utf-8"), title="最后 FEEDBACK"))

    # 日志末尾
    log_file = project_path / "logs" / f"{task_id}.log"
    if log_file.exists():
        tail = read_tail(log_file, n_bytes=2048)
        console.print(Panel(tail, title="log tail"))
