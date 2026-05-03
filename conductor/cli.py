"""Conductor CLI 入口（基于 typer）。

注意：实际命令实现分散在子模块里——driver/escalation/cost/memory 等模块各自
导出一个被 cli.py 装配的处理函数。本文件只负责命令路由、参数解析和退出码。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="conductor",
    help="Multi-Agent automated development orchestration system.",
    no_args_is_help=True,
)
workers_app = typer.Typer(name="workers", help="管理 Worker 类型（内置 + 自定义）。")
guardrails_app = typer.Typer(name="guardrails", help="项目护栏管理。")
app.add_typer(workers_app, name="workers")
app.add_typer(guardrails_app, name="guardrails")

console = Console()


# ---------- 项目根目录解析 ----------

def _projects_root() -> Path:
    """返回 projects/ 根目录。优先用环境变量，否则用 CWD/projects。"""
    root = os.environ.get("CONDUCTOR_PROJECTS_ROOT")
    if root:
        return Path(root)
    return Path.cwd() / "projects"


def _project_path(project_id: str) -> Path:
    return _projects_root() / project_id


# ---------- 项目生命周期 ----------

@app.command("init")
def init_project(
    project_id: str = typer.Argument(..., help="项目唯一标识（也是目录名）"),
    requirement: Optional[str] = typer.Option(None, "--requirement", "-r", help="需求文本"),
    requirement_file: Optional[Path] = typer.Option(
        None, "--requirement-file", "-f", help="需求文件路径"
    ),
    budget: float = typer.Option(50.0, "--budget", "-b", help="预算上限 USD"),
    max_concurrent: int = typer.Option(3, "--max-concurrent", "-c", help="最大并发 Worker 数"),
):
    """创建新项目。PM 会自主生成 PROJECT.md / GUARDRAILS.md / TASKS.json，
    然后暂停等待 Boss 确认。"""
    from conductor.project_init import init_project as _init

    if not requirement and not requirement_file:
        console.print("[red]错误：必须提供 --requirement 或 --requirement-file[/red]")
        raise typer.Exit(2)
    if requirement_file:
        requirement = requirement_file.read_text(encoding="utf-8")

    _init(
        project_id=project_id,
        requirement=requirement,
        budget=budget,
        max_concurrent=max_concurrent,
        projects_root=_projects_root(),
    )


@app.command("start")
def start_driver(project_id: str = typer.Argument(...)):
    """启动 driver loop，让 PM 自主推进项目。"""
    from conductor.driver import Driver

    driver = Driver(_project_path(project_id))
    result = driver.run()
    console.print(f"[bold]Driver 退出：{result}[/bold]")


@app.command("pause")
def pause_project(project_id: str = typer.Argument(...)):
    """暂停项目（设置 paused 状态，driver 下轮检查时退出）。"""
    from conductor.project_store import ProjectStore

    ProjectStore(_project_path(project_id)).set_state("paused")
    console.print(f"[yellow]项目 {project_id} 已暂停[/yellow]")


@app.command("resume")
def resume_project(project_id: str = typer.Argument(...)):
    """恢复并重新启动 driver。"""
    from conductor.driver import Driver
    from conductor.project_store import ProjectStore

    ProjectStore(_project_path(project_id)).set_state("running")
    Driver(_project_path(project_id)).run()


@app.command("stop")
def stop_project(project_id: str = typer.Argument(...)):
    """停止项目并 kill 所有正在跑的 worker。"""
    from conductor.process_group import cleanup_stale_workers
    from conductor.project_store import ProjectStore

    project_path = _project_path(project_id)
    n = cleanup_stale_workers(project_path)
    ProjectStore(project_path).set_state("stopped")
    console.print(f"[yellow]已停止 {project_id}，清理 {n} 个残留 worker[/yellow]")


# ---------- 状态查询 ----------

@app.command("status")
def show_status(project_id: str = typer.Argument(...)):
    """打印项目状态快照。"""
    from conductor.status_view import render_status

    render_status(_project_path(project_id), console)


@app.command("watch")
def watch_status(
    project_id: str = typer.Argument(...),
    interval: int = typer.Option(5, "--interval", "-i", help="刷新间隔秒"),
):
    """实时刷新项目状态。"""
    import time

    from conductor.status_view import render_status

    try:
        while True:
            console.clear()
            render_status(_project_path(project_id), console)
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]中断观察。[/yellow]")


@app.command("list")
def list_projects():
    """列出 projects/ 下所有项目及状态。"""
    from conductor.status_view import list_projects as _list

    _list(_projects_root(), console)


@app.command("logs")
def show_logs(
    project_id: str = typer.Argument(...),
    tail: int = typer.Option(50, "--tail", help="最后 N 行"),
    task: Optional[str] = typer.Option(None, "--task", help="只看某个 task 的日志"),
):
    from conductor.status_view import show_logs as _logs

    _logs(_project_path(project_id), tail=tail, task=task, console=console)


@app.command("decisions")
def show_decisions(project_id: str = typer.Argument(...), tail: int = typer.Option(20)):
    from conductor.status_view import show_decisions as _show

    _show(_project_path(project_id), tail=tail, console=console)


# ---------- Boss 交互 ----------

@app.command("reply")
def reply_to_pm(
    project_id: str = typer.Argument(...),
    message: str = typer.Argument(...),
):
    """回复最新一条待 Boss 处理的 escalation。"""
    from conductor.escalation import EscalationStore

    store = EscalationStore(_project_path(project_id))
    n = store.reply_latest(message)
    console.print(f"[green]已回复 {n} 条 escalation[/green]")


@app.command("escalations")
def list_escalations(project_id: str = typer.Argument(...)):
    from conductor.escalation import EscalationStore

    store = EscalationStore(_project_path(project_id))
    for e in store.list_pending():
        console.print(f"  - [{e['id']}] {e['title']} ({e['created_at']})")


# ---------- 护栏管理 ----------

@guardrails_app.command("show")
def guardrails_show(project_id: str = typer.Argument(...)):
    p = _project_path(project_id) / "GUARDRAILS.md"
    if not p.exists():
        console.print("[red]GUARDRAILS.md 不存在[/red]")
        raise typer.Exit(1)
    console.print(p.read_text(encoding="utf-8"))


@guardrails_app.command("validate")
def guardrails_validate(project_id: str = typer.Argument(...)):
    from conductor.guardrails import GuardrailsChecker

    p = _project_path(project_id) / "GUARDRAILS.md"
    errors = GuardrailsChecker.validate_file(p)
    if errors:
        for e in errors:
            console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1)
    console.print("[green]✓ GUARDRAILS.md 校验通过[/green]")


@guardrails_app.command("edit")
def guardrails_edit(project_id: str = typer.Argument(...)):
    """用 $EDITOR 打开 GUARDRAILS.md。"""
    import subprocess

    p = _project_path(project_id) / "GUARDRAILS.md"
    editor = os.environ.get("EDITOR", "notepad" if sys.platform == "win32" else "vi")
    subprocess.run([editor, str(p)])


# ---------- Worker 管理 ----------

@workers_app.command("list")
def workers_list():
    from conductor.workers.registry import WorkerRegistry

    WorkerRegistry.load_user_workers()
    builtins = ["claude_code", "codex", "gemini"]
    console.print("[bold]内置 Worker:[/bold]")
    for name in builtins:
        console.print(f"  {name}")
    custom = [n for n in WorkerRegistry.list_all() if n not in builtins]
    if custom:
        console.print("[bold]自定义 Worker:[/bold]")
        for name in custom:
            console.print(f"  {name}")


@workers_app.command("test")
def workers_test(name: str = typer.Argument(...)):
    """测试某 Worker 类型是否可用（构造命令、检查 binary 存在）。"""
    from conductor.workers.registry import WorkerRegistry

    WorkerRegistry.load_user_workers()
    try:
        worker = WorkerRegistry.get(name)
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1)

    import shutil

    cmd = worker.build_command(Path.cwd())
    binary = cmd[0] if cmd else None
    if binary and shutil.which(binary):
        console.print(f"[green]✓[/green] {name} 二进制可调用 ({shutil.which(binary)})")
    else:
        console.print(f"[red]✗[/red] {name} 二进制 {binary} 不在 PATH 中")
    console.print(f"[green]✓[/green] 注册类继承 WorkerDispatcher")
    console.print(f"[green]✓[/green] build_command 返回: {cmd}")


# ---------- 调试 ----------

@app.command("memory")
def show_memory(
    project_id: str = typer.Argument(...),
    history: bool = typer.Option(False, "--history", help="列出历史快照"),
):
    project_path = _project_path(project_id)
    if history:
        hist_dir = project_path / "MEMORY.history"
        if not hist_dir.exists():
            console.print("[yellow]无历史快照[/yellow]")
            return
        for f in sorted(hist_dir.iterdir()):
            console.print(f"  {f.name}")
    else:
        m = project_path / "MEMORY.md"
        if m.exists():
            console.print(m.read_text(encoding="utf-8"))
        else:
            console.print("[red]MEMORY.md 不存在[/red]")


@app.command("cost")
def show_cost(project_id: str = typer.Argument(...)):
    from conductor.cost import CostTracker

    tracker = CostTracker(_project_path(project_id))
    tracker.print_summary(console)


@app.command("pool")
def show_pool(project_id: str = typer.Argument(...)):
    """显示当前 worker pool 状态。"""
    import json

    pool_file = _project_path(project_id) / ".pm" / "worker_pool.json"
    if not pool_file.exists():
        console.print("[yellow]无 pool 状态文件（项目未运行过）[/yellow]")
        return
    state = json.loads(pool_file.read_text(encoding="utf-8"))
    console.print(json.dumps(state, indent=2, ensure_ascii=False))


@app.command("inspect")
def inspect_task(
    project_id: str = typer.Argument(...),
    task: str = typer.Option(..., "--task", help="task_id"),
):
    """查看某 task 的详细状态、日志、最后反馈。"""
    from conductor.status_view import inspect_task as _inspect

    _inspect(_project_path(project_id), task_id=task, console=console)


# ---------- 危险操作 ----------

@app.command("reset")
def reset_project(
    project_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="跳过确认"),
):
    """重置项目（删除 .pm 状态、worktrees、logs，保留源 markdown）。"""
    import shutil

    if not yes:
        console.print("[red]危险操作。加 --yes 确认。[/red]")
        raise typer.Exit(1)
    p = _project_path(project_id)
    for sub in [".pm", "worktrees", "logs", "MEMORY.history"]:
        target = p / sub
        if target.exists():
            shutil.rmtree(target)
    console.print(f"[yellow]项目 {project_id} 已重置[/yellow]")


@app.command("kill")
def kill_workers(project_id: str = typer.Argument(...)):
    """强制 kill 所有 worker 进程。"""
    from conductor.process_group import cleanup_stale_workers

    n = cleanup_stale_workers(_project_path(project_id))
    console.print(f"[yellow]已 kill {n} 个 worker 进程[/yellow]")


if __name__ == "__main__":
    app()
