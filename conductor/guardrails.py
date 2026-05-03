"""GuardrailsChecker：项目护栏的解析、PM 决策的兜底校验。

设计要点（见 PRD F11 / TDD §4.6）：
- GUARDRAILS.md 是双层格式：人类描述 + 嵌入式 yaml 块
- 解析器只看带 `# rules: <category>` 注释的 yaml 块
- 必含 4 个类别：tech_stack / scope / security / must_escalate
- driver 每轮 PM 决策后调 check() 兜底拦截违反护栏的决策
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from conductor.project_store import ProjectStore


# 抓出形如 ```yaml\n# rules: <category>\n<yaml body>\n``` 的代码块
_YAML_BLOCK_RE = re.compile(
    r"```yaml\s*\n(\s*#\s*rules:\s*([a-z_]+)\s*\n.*?)```",
    re.DOTALL,
)

REQUIRED_CATEGORIES = {"tech_stack", "scope", "security", "must_escalate"}


class GuardrailsParseError(Exception):
    """GUARDRAILS.md 格式不合法时抛出，driver 拒绝启动。"""


class GuardrailsChecker:
    def __init__(self, project: "ProjectStore"):
        self.project = project
        self.path = project.guardrails_md
        self.rules = self._parse_guardrails(self.path)

    @classmethod
    def _parse_guardrails(cls, path: Path) -> dict:
        """解析 GUARDRAILS.md，只看带 `# rules: <cat>` 注释的 yaml 块。

        自然语言描述段被忽略——它们是给人看的。
        """
        if not path.exists():
            raise GuardrailsParseError(f"{path} 不存在，PM 必须先生成")

        text = path.read_text(encoding="utf-8")
        rules: dict = {}
        seen_categories: set[str] = set()

        for body, category in _YAML_BLOCK_RE.findall(text):
            try:
                data = yaml.safe_load(body) or {}
            except yaml.YAMLError as e:
                raise GuardrailsParseError(
                    f"yaml 块 '{category}' 语法错误: {e}"
                )
            if not isinstance(data, dict):
                raise GuardrailsParseError(
                    f"yaml 块 '{category}' 必须是映射类型，得到 {type(data).__name__}"
                )
            seen_categories.add(category)
            rules.update(data)  # 各类别的键平铺到一个字典

        missing = REQUIRED_CATEGORIES - seen_categories
        if missing:
            raise GuardrailsParseError(
                f"缺失必需类别: {sorted(missing)}。请在 GUARDRAILS.md 中补全这些 yaml 块。"
            )
        return rules

    # ---------- decision 兜底校验 ----------

    def check(self, decision: dict) -> list[str]:
        """检查 PM 决策是否违反护栏，返回违反条款列表（空列表 = 通过）。

        检查项：
        1. forbidden_dependencies — worker prompt 不得提到禁用依赖
        2. out_of_scope — worker prompt 不得提到超范围功能
        3. forbidden_patterns — worker prompt 不得包含可疑代码片段
        4. must_escalate — 决策若涉及红线必须 action=escalate
        """
        violations: list[str] = []

        # 抽取要派的所有 prompts（dispatch / dispatch_parallel）
        prompts = self._extract_prompts(decision)

        for prompt in prompts:
            for dep in self.rules.get("forbidden_dependencies", []) or []:
                if self._mentions_dependency(prompt, dep):
                    violations.append(f"forbidden_dependency:{dep}")

            for scope in self.rules.get("out_of_scope", []) or []:
                if self._mentions_scope(prompt, scope):
                    violations.append(f"out_of_scope:{scope}")

            for pattern in self.rules.get("forbidden_patterns", []) or []:
                try:
                    if re.search(pattern, prompt):
                        violations.append(f"forbidden_pattern:{pattern}")
                except re.error:
                    # pattern 本身不合法——忽略，不阻断决策
                    pass

        # must_escalate 项：若决策内容涉及这些字面词且 action != escalate
        if self._intends_to(decision, self.rules.get("must_escalate", []) or []):
            if decision.get("action") != "escalate":
                violations.append("must_escalate_violated")

        return violations

    @staticmethod
    def _extract_prompts(decision: dict) -> list[str]:
        action = decision.get("action")
        if action == "dispatch":
            return [decision.get("prompt", "")]
        if action == "dispatch_parallel":
            return [item.get("prompt", "") for item in decision.get("batch", [])]
        # 其它 action 不需要检查 prompt
        return []

    @staticmethod
    def _mentions_dependency(prompt: str, dep: str) -> bool:
        """判断 prompt 是否提到某依赖。

        简单实现：用单词边界匹配（避免把 'redis' 误判为 'redux' 触发）。
        """
        if not dep or not prompt:
            return False
        # 用单词边界，case-insensitive
        return bool(re.search(rf"\b{re.escape(dep)}\b", prompt, re.IGNORECASE))

    @staticmethod
    def _mentions_scope(prompt: str, scope: str) -> bool:
        if not scope or not prompt:
            return False
        return scope.lower() in prompt.lower()

    @staticmethod
    def _intends_to(decision: dict, must_escalate_items: list[str]) -> bool:
        """启发式：扫描 decision 的 reasoning / prompt / batch.prompt，
        看是否提到 must_escalate 列表里的关键词。

        关键词通常是 "introduce_new_framework" 这种 snake_case；
        匹配两种形式（snake_case 原样 + 空格分隔的词组）以兼容 PM 的两种写法。
        """
        if not must_escalate_items:
            return False
        haystack_parts = [
            decision.get("reasoning", ""),
            decision.get("prompt", ""),
            decision.get("escalation_reason", ""),
        ]
        for item in decision.get("batch", []) or []:
            haystack_parts.append(item.get("prompt", ""))
        haystack = " ".join(haystack_parts).lower()
        for item in must_escalate_items:
            snake = item.lower()
            spaced = item.replace("_", " ").lower()
            if snake and snake in haystack:
                return True
            if spaced and spaced != snake and spaced in haystack:
                return True
        return False

    # ---------- 注入 PM prompt ----------

    def inject_into_pm_prompt(self) -> str:
        """生成注入 PM system prompt 的护栏文本。

        直接给原文（人类描述+yaml），让 PM 同时看到"为什么"和"机器规则"。
        """
        if not self.path.exists():
            return "（GUARDRAILS.md 缺失）"
        return f"""# 项目护栏（你必须遵守）

{self.path.read_text(encoding='utf-8')}

# 重要规则
- 上文 yaml 块中带 `# rules: must_escalate` 类别的事项，必须输出 action="escalate"
- 触及 tech_stack / scope 类别红线的任务直接拒绝拆分
- 你的决策会被 driver 二次校验，违反护栏的决策会被拒绝执行
- yaml 块是机器规则，自然语言段是给你和 Boss 看的"为什么"
"""

    # ---------- 静态校验（CLI 子命令调用）----------

    @staticmethod
    def validate_file(path: Path) -> list[str]:
        """供 `conductor guardrails --validate` 调用的静态校验。

        返回错误消息列表（空列表 = 校验通过）。
        """
        try:
            GuardrailsChecker._parse_guardrails(path)
            return []
        except GuardrailsParseError as e:
            return [str(e)]
