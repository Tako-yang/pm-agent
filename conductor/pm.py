"""PM Agent：单轮决策模型，不持有循环控制权。

每次 decide_once：
  输入：state + memory + recent feedback + GUARDRAILS + pool_status
  输出：JSON decision（dispatch / dispatch_parallel / verify / replan / complete /
        escalate / distill_memory）

decide_once 完成即返回——driver 来决定是否进入下一轮。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from jsonschema import ValidationError, validate as _validate

from conductor.utils import iso_now, load_json_schema, load_prompt

if TYPE_CHECKING:
    from conductor.project_store import ProjectStore


DECISION_SCHEMA = load_json_schema("decision.schema.json")

# 默认模型——可被环境变量覆盖以适配未来模型升级
DEFAULT_PM_MODEL = os.environ.get("CONDUCTOR_PM_MODEL", "claude-opus-4-5")
PM_MAX_TOKENS = int(os.environ.get("CONDUCTOR_PM_MAX_TOKENS", "4096"))


class PMError(Exception):
    """PM 决策异常（API 错误、JSON 解析失败、schema 校验失败等）。"""


@dataclass
class Decision:
    """单轮 PM 决策。本质是个 dict 包装；提供属性访问便利。"""

    action: str
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        return cls(action=data.get("action", ""), raw=data)

    def to_dict(self) -> dict:
        return self.raw


class PMAgent:
    """PM Agent —— 单轮决策模型。

    认证：直接用 ANTHROPIC_API_KEY 走 Anthropic API（不通过 worker 模式）。
    """

    def __init__(
        self,
        project: "ProjectStore",
        guardrails: Optional["GuardrailsChecker"] = None,
        client=None,
    ):
        self.project = project
        # 延迟 import 避免在没装 anthropic 时也能 import pm 做单测
        if client is None:
            from anthropic import Anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise PMError("环境变量 ANTHROPIC_API_KEY 未设置——PM 无法启动")
            client = Anthropic(api_key=api_key)
        self.client = client
        if guardrails is None:
            from conductor.guardrails import GuardrailsChecker
            guardrails = GuardrailsChecker(project)
        self.guardrails = guardrails

    # ---------- 主决策方法 ----------

    def decide_once(
        self,
        available_workers: int = 1,
        pool_status: Optional[dict] = None,
        recent_events: Optional[list[dict]] = None,
    ) -> Decision:
        """单轮决策。"""
        from conductor.memory import StructuredMemory

        memory = StructuredMemory.load(self.project.memory_md)

        # 检查 MEMORY 是否超限（优先返回蒸馏决策）
        if memory.needs_distillation():
            decision = Decision.from_dict({"action": "distill_memory"})
            self.project.append_decision_log({"event": "decision", "decision": decision.to_dict()})
            return decision

        # 收集输入
        tasks = self.project.tasks.all()
        recent_feedback = self.project.load_recent_feedback(n=5)
        pending_corrections = self._load_pending_corrections()

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            memory_text=memory.to_markdown(),
            tasks_text=self._render_tasks(tasks),
            recent_feedback=recent_feedback,
            pending_corrections=pending_corrections,
            available_workers=available_workers,
            pool_status=pool_status or {},
            recent_events=recent_events or [],
        )

        # 调 Claude API（带一次重试以应对偶发的 JSON 格式错）
        last_err: Optional[str] = None
        for attempt in range(2):
            try:
                response = self.client.messages.create(
                    model=DEFAULT_PM_MODEL,
                    max_tokens=PM_MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                text = response.content[0].text if response.content else ""
                decision_dict = self._parse_decision(text)
                self.project.append_decision_log({
                    "event": "decision",
                    "iter_attempt": attempt,
                    "decision": decision_dict,
                })
                return Decision.from_dict(decision_dict)
            except (PMError, ValidationError, json.JSONDecodeError) as e:
                last_err = str(e)
                # 第二轮加严格指令再让 PM 重试
                user_prompt = (
                    user_prompt
                    + f"\n\n[SYSTEM] 上次输出未通过 schema 校验：{last_err}。"
                    + "请严格按 schema 输出 JSON，不要任何额外文本。"
                )
        raise PMError(f"PM 决策连续 2 次解析失败: {last_err}")

    # ---------- 蒸馏 ----------

    def distill_memory(self) -> None:
        """专门的蒸馏轮：把超限的 MEMORY.md 压缩到 ≤ 2500 字。"""
        from conductor.memory import StructuredMemory

        memory = StructuredMemory.load(self.project.memory_md)
        # 归档旧版本
        memory.archive(self.project.path / "MEMORY.history")

        prompt_tmpl = load_prompt("distill.md")
        user_msg = prompt_tmpl.replace("{{old_memory}}", memory.to_markdown())

        response = self.client.messages.create(
            model=DEFAULT_PM_MODEL,
            max_tokens=PM_MAX_TOKENS,
            system="你是 MEMORY.md 蒸馏专家。",
            messages=[{"role": "user", "content": user_msg}],
        )
        new_text = response.content[0].text if response.content else ""

        # 校验：必须包含全部 5 段
        new_memory = StructuredMemory.parse(new_text)
        for sec in StructuredMemory.SECTIONS:
            if sec not in new_memory.sections:
                # 蒸馏失败——不写入，记录日志
                self.project.append_decision_log({
                    "event": "distill_failed",
                    "missing_section": sec,
                })
                return
        new_memory.save(self.project.memory_md)
        self.project.append_decision_log({
            "event": "distill_success",
            "new_size": new_memory.total_chars(),
        })

    # ---------- 解析 PM 输出 ----------

    @staticmethod
    def _parse_decision(text: str) -> dict:
        """从 PM 文本输出中提取 JSON 决策并校验 schema。

        容错：剥掉可能的 markdown 代码栅。
        """
        if not text:
            raise PMError("PM 返回空文本")

        cleaned = text.strip()
        # 剥代码栅
        m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()
        # 兜底：找第一个 { 到最后一个 }
        if not cleaned.startswith("{"):
            i, j = cleaned.find("{"), cleaned.rfind("}")
            if i >= 0 and j > i:
                cleaned = cleaned[i:j + 1]

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise PMError(f"JSON 解析失败: {e}")

        _validate(data, DECISION_SCHEMA)
        return data

    # ---------- prompt 构造 ----------

    def _build_system_prompt(self) -> str:
        try:
            tmpl = load_prompt("pm_system.md")
        except FileNotFoundError:
            tmpl = self._default_system_prompt()
        injected_guardrails = self.guardrails.inject_into_pm_prompt()
        injected_project_md = self.project.load_project_md() or "（PROJECT.md 缺失）"
        return (
            tmpl.replace("{{INJECTED_GUARDRAILS}}", injected_guardrails)
            .replace("{{INJECTED_PROJECT_MD}}", injected_project_md)
        )

    def _build_user_prompt(
        self,
        memory_text: str,
        tasks_text: str,
        recent_feedback: list[dict],
        pending_corrections: list[dict],
        available_workers: int,
        pool_status: dict,
        recent_events: list[dict],
    ) -> str:
        return f"""
# 当前 MEMORY.md
{memory_text}

# 当前 TASKS.json 摘要
{tasks_text}

# 最近 worker 反馈（最多 5 条）
{json.dumps(recent_feedback, indent=2, ensure_ascii=False)}

# 待双重确认的 memory_corrections
{json.dumps(pending_corrections, indent=2, ensure_ascii=False)}

# 当前 worker pool 状态
- 空闲槽位: {available_workers}
- 运行中: {json.dumps(pool_status.get('active', []), ensure_ascii=False)}

# 最近系统事件（partial_batch_rejected / worker_killed_by_monitor 等）
{json.dumps(recent_events, indent=2, ensure_ascii=False)}

# 时间
{iso_now()}

请输出严格符合 decision.schema.json 的 JSON。
""".strip()

    @staticmethod
    def _render_tasks(tasks: list) -> str:
        if not tasks:
            return "（任务列表为空）"
        lines = []
        for t in tasks:
            lines.append(
                f"- [{t.status}] {t.id}: {t.title}"
                + (f" (cli={t.assigned_cli})" if t.assigned_cli else "")
                + (f" deps={t.depends_on}" if t.depends_on else "")
                + (f" attempts={t.attempts}" if t.attempts else "")
            )
        return "\n".join(lines)

    def _load_pending_corrections(self) -> list[dict]:
        if not self.project.pending_corrections.exists():
            return []
        try:
            data = json.loads(self.project.pending_corrections.read_text(encoding="utf-8"))
            return [p for p in data if not p.get("resolved")]
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def _default_system_prompt() -> str:
        # fallback 当 prompts/pm_system.md 还没生成时
        return """你是高度自主的产品经理 AI，负责自动推进软件项目开发。

# 你的红线
{{INJECTED_GUARDRAILS}}

# 项目宪法
{{INJECTED_PROJECT_MD}}

输出严格 JSON，符合 decision.schema.json。
"""
