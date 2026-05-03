"""项目初始化：conductor init 命令的实现。

流程：
1. 创建 project 目录结构
2. 调 PM 一次性生成 PROJECT.md / GUARDRAILS.md / TASKS.json
3. 校验 GUARDRAILS.md 格式（必含 4 个 yaml 类别）
4. 创建 escalation 让 Boss 确认
5. 等待 Boss 通过 conductor reply 回复 → 之后 conductor start 启动 driver
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from conductor.cost import CostTracker
from conductor.escalation import EscalationStore
from conductor.guardrails import GuardrailsChecker, GuardrailsParseError
from conductor.project_store import ProjectStore
from conductor.utils import iso_now, load_prompt
from conductor.worktree import WorktreeManager


# PM 输出的 guardrails_md 块内含嵌套 yaml 代码栅，无法用 3 反引号区分外层
# 与内层。我们要求外层用 4 反引号，内层 yaml 用 3 反引号。
# 优先匹配 4 反引号；若失败 fall back 到 3 反引号（适合 project_md / tasks_json 这种无嵌套的）。
_BLOCK_RE_4 = re.compile(r"````(\w+)\s*\n(.*?)\n?````", re.DOTALL)
_BLOCK_RE_3 = re.compile(r"```(\w+)\s*\n(.*?)\n?```", re.DOTALL)


def init_project(
    project_id: str,
    requirement: str,
    budget: float = 50.0,
    max_concurrent: int = 3,
    projects_root: Optional[Path] = None,
    pm_client=None,
) -> Path:
    """完成 conductor init 的所有步骤。返回 project 路径。"""
    projects_root = projects_root or Path.cwd() / "projects"
    project_path = projects_root / project_id

    if project_path.exists() and (project_path / "PROJECT.md").exists():
        raise RuntimeError(f"项目 {project_id} 已存在。用 conductor reset 重置或换 id。")

    print(f"[Conductor] 创建项目 {project_id} ...")
    project = ProjectStore(project_path)

    # 设置预算
    cost = CostTracker(project_path, budget=budget)
    cost.set_budget(budget)

    # 保存初始配置
    project.save_state({
        "phase": "init",
        "project_id": project_id,
        "max_concurrent": max_concurrent,
        "budget": budget,
        "requirement": requirement,
        "created_at": iso_now(),
    })

    # 调 PM 生成三件套
    print("[Conductor] PM 正在生成 PROJECT.md / GUARDRAILS.md / TASKS.json ...")
    project_md, guardrails_md, tasks_json_text = _call_pm_for_init(
        project_id, requirement, client=pm_client,
    )
    project.write_project_md(project_md)
    project.write_guardrails_md(guardrails_md)

    # 校验 TASKS.json 是合法 JSON
    try:
        tasks_data = json.loads(tasks_json_text)
        # 强制 project_id 一致
        tasks_data["project_id"] = project_id
        tasks_data.setdefault("version", 1)
        project.tasks_json.write_text(
            json.dumps(tasks_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PM 生成的 TASKS.json 不是合法 JSON: {e}")

    # 校验 GUARDRAILS.md 格式
    try:
        GuardrailsChecker._parse_guardrails(project.guardrails_md)
    except GuardrailsParseError as e:
        # 用 ASCII 字符避免 Windows GBK 控制台编码问题
        print(f"[Conductor] [!] GUARDRAILS.md 格式问题: {e}")
        print("  Boss 编辑 GUARDRAILS.md 修复后再 conductor reply approved")

    # 初始化 git 仓库（init 阶段就要建好，方便后续 worktree）
    WorktreeManager(project_path).ensure_repo()

    # 写一份空白 MEMORY.md
    from conductor.memory import StructuredMemory
    StructuredMemory({s: "" for s in StructuredMemory.SECTIONS}).save(project.memory_md)

    # 创建初始化 escalation
    escalation = EscalationStore(project_path)
    escalation.create(
        title="项目宪法和护栏已生成，请确认",
        body=(
            f"PM 已生成以下三个文件，请审阅并确认：\n\n"
            f"- {project.project_md}（项目宪法）\n"
            f"- {project.guardrails_md}（项目护栏）\n"
            f"- {project.tasks_json}（{len(tasks_data.get('tasks', []))} 个任务）\n\n"
            f"如需修改：编辑文件后运行 conductor reply {project_id} approved\n"
            f"如要重新生成：conductor reset {project_id} --yes 后重新 init"
        ),
    )

    print(f"[Conductor] [OK] 项目 {project_id} 已创建")
    print(f"  → {project_path}")
    print(f"  审阅 PROJECT.md / GUARDRAILS.md 后确认: conductor reply {project_id} approved")
    return project_path


def _call_pm_for_init(project_id: str, requirement: str, client=None) -> tuple[str, str, str]:
    """让 PM 一次性生成 PROJECT.md / GUARDRAILS.md / TASKS.json。

    返回 (project_md, guardrails_md, tasks_json_text) 三元组。
    """
    if client is None:
        from anthropic import Anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("环境变量 ANTHROPIC_API_KEY 未设置——无法初始化 PM")
        client = Anthropic(api_key=api_key)

    prompt_tmpl = load_prompt("init_pm.md")
    user_msg = prompt_tmpl.replace("{{user_requirement}}", requirement)

    from conductor.pm import DEFAULT_PM_MODEL, PM_MAX_TOKENS

    response = client.messages.create(
        model=DEFAULT_PM_MODEL,
        max_tokens=PM_MAX_TOKENS,
        system="你正在为新项目做启动规划。请严格按要求输出三个 markdown 代码块。",
        messages=[{"role": "user", "content": user_msg}],
    )
    text = response.content[0].text if response.content else ""

    # 解析三个块：先抓 4 反引号块（适合内含嵌套代码栅的 guardrails_md），
    # 余下文本再抓 3 反引号块（适合 project_md / tasks_json）。
    blocks: dict[str, str] = {}
    remaining = text
    for tag, body in _BLOCK_RE_4.findall(text):
        blocks[tag] = body.strip()
    # 把已匹配的部分挖掉，避免下一轮重复匹配
    remaining = _BLOCK_RE_4.sub("", text)
    for tag, body in _BLOCK_RE_3.findall(remaining):
        if tag not in blocks:  # 4 反引号优先
            blocks[tag] = body.strip()

    project_md = blocks.get("project_md", "").strip()
    guardrails_md = blocks.get("guardrails_md", "").strip()
    tasks_json = blocks.get("tasks_json", "").strip()

    missing = [name for name, val in [
        ("project_md", project_md),
        ("guardrails_md", guardrails_md),
        ("tasks_json", tasks_json),
    ] if not val]
    if missing:
        raise RuntimeError(
            f"PM 输出缺失代码块: {missing}。完整输出:\n{text[:1000]}"
        )

    return project_md, guardrails_md, tasks_json
