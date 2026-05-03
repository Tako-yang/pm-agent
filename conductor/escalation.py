"""Boss 升级机制：escalations/<NNN>_<title>.md 文件 + 终端通知 + reply 回复。

文件格式：
    # <title>
    Created at: <iso>
    Status: pending | replied

    ## 详情
    <body>

    ## 决策快照（可选）
    ```json
    <decision dict>
    ```

    ## RESPONSE
    <Boss 编辑此段或用 conductor reply 命令填入>
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from conductor.utils import iso_now


_FILENAME_RE = re.compile(r"^(\d{3,})_([a-z0-9_\-]+)\.md$", re.IGNORECASE)


class EscalationStore:
    def __init__(self, project_path: Path):
        self.project_path = Path(project_path)
        self.dir = self.project_path / "escalations"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._created_keys: set[str] = set()  # 同进程去重

    # ---------- 创建 ----------

    def create(
        self,
        title: str,
        body: str = "",
        decision: Optional[dict] = None,
    ) -> Path:
        """创建一个新的 escalation 文件。"""
        n = self._next_index()
        slug = self._slugify(title)
        path = self.dir / f"{n:03d}_{slug}.md"
        content = self._render(title, body, decision, status="pending")
        path.write_text(content, encoding="utf-8")
        self._notify_terminal(path, title)
        return path

    def create_once(self, key: str, title: str, body: str = "") -> Optional[Path]:
        """同 key 只创建一次（同进程内 + 检查 escalations 目录）。"""
        if key in self._created_keys:
            return None
        # 也扫一下目录，避免重启后重复创建
        for f in self.dir.iterdir():
            if f.is_file() and f.name.endswith(f"{self._slugify(key)}.md"):
                self._created_keys.add(key)
                return None
        self._created_keys.add(key)
        return self.create(title=title, body=body)

    # ---------- 查询 ----------

    def list_pending(self) -> list[dict]:
        """列出所有未回复的 escalation 元数据。"""
        results = []
        for f in sorted(self.dir.iterdir()):
            if not f.is_file() or not f.name.endswith(".md"):
                continue
            meta = self._parse_meta(f)
            if meta and meta["status"] == "pending":
                results.append(meta)
        return results

    def list_all(self) -> list[dict]:
        results = []
        for f in sorted(self.dir.iterdir()):
            if not f.is_file() or not f.name.endswith(".md"):
                continue
            meta = self._parse_meta(f)
            if meta:
                results.append(meta)
        return results

    # ---------- 回复 ----------

    def reply_latest(self, message: str) -> int:
        """回复最新的待回复 escalation。返回回复的数量（0 或 1）。"""
        pending = self.list_pending()
        if not pending:
            return 0
        latest = pending[-1]
        path = Path(latest["path"])
        text = path.read_text(encoding="utf-8")

        # 替换状态行
        text = re.sub(
            r"^Status:\s*pending\s*$",
            "Status: replied",
            text,
            count=1,
            flags=re.MULTILINE,
        )

        # 追加 RESPONSE 段
        if "## RESPONSE" in text:
            text = re.sub(
                r"## RESPONSE\s*\n.*?(?=\n##|\Z)",
                f"## RESPONSE\n{message}\n\n_replied at {iso_now()}_\n",
                text,
                count=1,
                flags=re.DOTALL,
            )
        else:
            text += f"\n\n## RESPONSE\n{message}\n\n_replied at {iso_now()}_\n"

        path.write_text(text, encoding="utf-8")
        return 1

    # ---------- 渲染/解析 ----------

    @staticmethod
    def _render(title: str, body: str, decision: Optional[dict], status: str) -> str:
        parts = [
            f"# {title}",
            f"Created at: {iso_now()}",
            f"Status: {status}",
            "",
            "## 详情",
            body or "（无）",
            "",
        ]
        if decision is not None:
            parts.extend([
                "## 决策快照",
                "```json",
                json.dumps(decision, indent=2, ensure_ascii=False),
                "```",
                "",
            ])
        parts.extend([
            "## RESPONSE",
            "_Boss 在此填写回复，或运行: conductor reply <project_id> \"...\"_",
            "",
        ])
        return "\n".join(parts)

    @staticmethod
    def _parse_meta(path: Path) -> Optional[dict]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        status_match = re.search(r"^Status:\s*(\w+)\s*$", text, re.MULTILINE)
        created_match = re.search(r"^Created at:\s*(.+)$", text, re.MULTILINE)
        m = _FILENAME_RE.match(path.name)
        return {
            "id": m.group(1) if m else path.stem,
            "title": title_match.group(1).strip() if title_match else path.stem,
            "status": status_match.group(1).strip() if status_match else "unknown",
            "created_at": created_match.group(1).strip() if created_match else "",
            "path": str(path),
        }

    # ---------- 工具 ----------

    def _next_index(self) -> int:
        max_n = 0
        for f in self.dir.iterdir():
            if not f.is_file():
                continue
            m = _FILENAME_RE.match(f.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return max_n + 1

    @staticmethod
    def _slugify(text: str) -> str:
        # 中文 + 半角符号 → 简单转 ascii slug
        text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE)
        text = re.sub(r"_+", "_", text).strip("_").lower()
        return text[:60] or "escalation"

    @staticmethod
    def _notify_terminal(path: Path, title: str) -> None:
        """终端打印 + 跨平台通知（best effort）。"""
        msg = f"\n[Conductor 升级] {title}\n  → {path}\n  请用: conductor reply <project_id> \"...\"\n"
        print(msg, flush=True)
        # 系统通知（best effort，不抛错）
        try:
            import sys
            if sys.platform == "darwin":
                import subprocess
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{title}" with title "Conductor"'],
                    capture_output=True, timeout=2,
                )
            elif sys.platform == "win32":
                # Windows toast：用 powershell BurntToast 太重，这里仅打印
                pass
            else:
                import shutil
                if shutil.which("notify-send"):
                    import subprocess
                    subprocess.run(
                        ["notify-send", "Conductor", title],
                        capture_output=True, timeout=2,
                    )
        except Exception:
            pass
