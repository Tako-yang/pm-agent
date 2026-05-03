"""StructuredMemory：MEMORY.md 5 段固定结构管理。

MVP 阶段硬约定 5 段（Boss 决策，详见 PRD F4 / TDD §3.4）：
    1. 项目宪法（永远不变）
    2. 当前架构（随实现演进）
    3. 已知坑（worker 必读，避免重复踩）
    4. 当前未完成任务的上下文（只放正在做的）
    5. 已完成里程碑（一句话）

不允许追加式累积——PM 必须整段重写。超过 3000 字触发蒸馏。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from conductor.utils import iso_now


class MemoryError(Exception):
    """MEMORY.md 操作相关错误。"""


class StructuredMemory:
    SECTIONS = [
        "项目宪法（永远不变）",
        "当前架构（随实现演进）",
        "已知坑（worker 必读，避免重复踩）",
        "当前未完成任务的上下文（只放正在做的）",
        "已完成里程碑（一句话）",
    ]
    MAX_CHARS = 3000
    DISTILL_TARGET_CHARS = 2500  # 蒸馏后留 500 字给后续追加

    def __init__(self, sections: dict[str, str]):
        # 不补全缺失段——保持 PM 输入的原貌，由 to_markdown() 渲染时填空字符串
        self.sections = sections

    # ---------- 加载/保存 ----------

    @classmethod
    def load(cls, path: Path) -> "StructuredMemory":
        if not path.exists():
            return cls({s: "" for s in cls.SECTIONS})
        text = path.read_text(encoding="utf-8")
        return cls.parse(text)

    @classmethod
    def parse(cls, text: str) -> "StructuredMemory":
        """从 markdown 文本解析。容错：未识别的段被忽略。"""
        sections = cls._split_sections(text)
        return cls(sections)

    @staticmethod
    def _split_sections(text: str) -> dict[str, str]:
        """按 `# <section_name>` 分段。

        实现要点：
        - 用 `^#\s+(.+?)$` 匹配段标题（允许 # 之间是单个空格）
        - 段内容是从标题下一行到下一个段标题之前的所有内容
        - 段名头尾去空白，便于后续 _resolve_section_name 匹配
        """
        result: dict[str, str] = {}
        # 先按段头切分
        parts = re.split(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
        # parts 形如 [前导杂项, 段名1, 内容1, 段名2, 内容2, ...]
        if len(parts) < 3:
            return result
        for i in range(1, len(parts), 2):
            name = parts[i].strip()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            # 去掉 markdown 注释
            content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).strip()
            result[name] = content
        return result

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(), encoding="utf-8")

    # ---------- 渲染 ----------

    def to_markdown(self, header_comment: Optional[str] = None) -> str:
        """渲染为 markdown 文本。永远按 5 段标准顺序输出，缺失段写空内容。"""
        lines: list[str] = []
        if header_comment:
            lines.append(f"<!-- {header_comment} -->")
        else:
            lines.append("<!-- MEMORY.md - 由 PM 每轮重写，不可手动追加 -->")
            lines.append(f"<!-- 大小上限 {self.MAX_CHARS} 字，超限触发蒸馏 -->")
            lines.append(f"<!-- 最后更新: {iso_now()} -->")
        lines.append("")

        for name in self.SECTIONS:
            content = self.sections.get(name, "").strip()
            lines.append(f"# {name}")
            lines.append("")
            lines.append(content if content else "_（暂无内容）_")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def total_chars(self) -> int:
        """字数统计——只数段内容（不含注释和段头），避免格式抖动影响判定。"""
        return sum(len(c) for c in self.sections.values())

    def needs_distillation(self) -> bool:
        return self.total_chars() > self.MAX_CHARS

    # ---------- 段名解析 ----------

    def _resolve_section_name(self, short_name: str) -> Optional[str]:
        """兼容缩写：'已知坑' → '已知坑（worker 必读，避免重复踩）'。

        规则：
        - 完全相等：返回原名
        - 段名以缩写开头：返回完整段名
        - 缩写包含在段名里：返回完整段名（fuzzy）
        - 否则：None
        """
        short = short_name.strip()
        for full in self.SECTIONS:
            if full == short:
                return full
        for full in self.SECTIONS:
            if full.startswith(short) or short in full:
                return full
        return None

    # ---------- 更新 ----------

    def apply_update(self, update: dict) -> None:
        """应用 worker 反馈中的单条 memory_updates 项。

        update = {"section": "已知坑", "action": "add|update|remove", "content": "..."}
        """
        section_short = update.get("section", "")
        section = self._resolve_section_name(section_short)
        if section is None:
            raise MemoryError(f"未知段名: {section_short}")

        action = update.get("action")
        content = update.get("content", "").strip()

        if action == "add":
            existing = self.sections.get(section, "").strip()
            new_line = content if content.startswith("-") else f"- {content}"
            self.sections[section] = (existing + ("\n" if existing else "") + new_line)
        elif action == "update":
            self.sections[section] = content
        elif action == "remove":
            existing = self.sections.get(section, "")
            self.sections[section] = "\n".join(
                line for line in existing.splitlines()
                if content not in line
            ).strip()
        else:
            raise MemoryError(f"未知 action: {action}")

    def apply_updates(self, updates: list[dict]) -> int:
        """批量应用 updates。返回成功应用的条数。
        单条失败不阻断其它——记录但继续，上层可在日志中看到。
        """
        n = 0
        for u in updates:
            try:
                self.apply_update(u)
                n += 1
            except MemoryError:
                # 静默吞——上层（pm.py）应记录到 memory.log
                pass
        return n

    def remove_completed_task_context(self, completed_task_ids: list[str]) -> None:
        """task 完成后从'当前未完成任务的上下文'段移除对应行。"""
        section = "当前未完成任务的上下文（只放正在做的）"
        existing = self.sections.get(section, "")
        kept_lines = [
            line for line in existing.splitlines()
            if not any(tid in line for tid in completed_task_ids)
        ]
        self.sections[section] = "\n".join(kept_lines).strip()

    # ---------- 注入 worker prompt ----------

    def inject_for_worker(self, sections_needed: list[str]) -> str:
        """只取 worker 需要的段，减少 token 浪费。

        参数:
            sections_needed: 段名列表（支持缩写）

        返回:
            按 5 段标准顺序拼接的 markdown 片段
        """
        resolved: list[str] = []
        for s in sections_needed:
            full = self._resolve_section_name(s)
            if full and full not in resolved:
                resolved.append(full)
        # 保持 5 段标准顺序
        ordered = [s for s in self.SECTIONS if s in resolved]
        parts = []
        for name in ordered:
            content = self.sections.get(name, "").strip()
            if content:
                parts.append(f"# {name}\n{content}")
        return "\n\n".join(parts)

    # ---------- 历史归档 ----------

    def archive(self, history_dir: Path) -> Path:
        """把当前内容写入历史目录，返回归档文件路径。"""
        history_dir.mkdir(parents=True, exist_ok=True)
        # 用文件名安全的时间戳（去掉 :）
        ts = iso_now().replace(":", "-")
        archive_path = history_dir / f"{ts}.md"
        archive_path.write_text(self.to_markdown(header_comment=f"归档于 {ts}"), encoding="utf-8")
        return archive_path
