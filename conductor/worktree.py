"""git worktree 管理：每个并发任务一个独立 worktree。

设计要点（详见 PRD F3 / TDD §4.3）：
- 隔离优先于性能：每任务独立 worktree，不允许两个 worker 共用
- 失败回滚锚点：派发前记录 HEAD SHA，失败时 reset --hard 回滚
- worktree 重建：失败 attempt 后 rebuild_worktree 彻底擦干净，避免上次残留
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(Exception):
    """worktree 操作失败。"""


class WorktreeManager:
    """管理 project 的 worktree 集合。

    project_root 必须是一个 git 仓库（init 阶段 driver 会保证这一点）。
    每个 task 对应 worktrees/<task_id>/。
    """

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.worktrees_dir = self.project_root / "worktrees"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    def ensure_repo(self) -> None:
        """保证 project_root 是 git 仓库。如果不是，初始化一个。

        init 阶段调用——PM 生成 PROJECT.md / GUARDRAILS.md 后由 driver 调用。
        """
        if (self.project_root / ".git").exists():
            return
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.project_root, check=True, capture_output=True,
        )
        # 必须有至少一个 commit 才能创建 worktree
        gitignore = self.project_root / ".gitignore"
        gitignore.write_text(
            "worktrees/\n.pm/\nlogs/\nMEMORY.history/\nescalations/\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=self.project_root, check=True, capture_output=True,
        )
        # 设置必要的 user 配置（如果全局未设置）
        self._ensure_git_identity()
        subprocess.run(
            ["git", "commit", "-m", "conductor: initial commit"],
            cwd=self.project_root, check=True, capture_output=True,
        )

    def _ensure_git_identity(self) -> None:
        """确保仓库内有 user.name / user.email。"""
        for key, default in [("user.name", "conductor"), ("user.email", "conductor@local")]:
            r = subprocess.run(
                ["git", "config", "--get", key],
                cwd=self.project_root, capture_output=True, text=True,
            )
            if r.returncode != 0 or not r.stdout.strip():
                subprocess.run(
                    ["git", "config", key, default],
                    cwd=self.project_root, check=True, capture_output=True,
                )

    def ensure_worktree(self, task_id: str) -> Path:
        """获取 task 对应的 worktree。不存在则创建。

        分支命名约定：task/<task_id>。如果分支已存在则直接 checkout。
        """
        wt_path = self.worktrees_dir / task_id
        if wt_path.exists() and (wt_path / ".git").exists():
            return wt_path

        # 兜底清理：目录残留但 .git 不存在
        if wt_path.exists():
            import shutil
            shutil.rmtree(wt_path, ignore_errors=True)

        branch = f"task/{task_id}"
        # 检查分支是否已存在
        r = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=self.project_root, capture_output=True, text=True,
        )
        if r.returncode == 0:
            cmd = ["git", "worktree", "add", str(wt_path), branch]
        else:
            cmd = ["git", "worktree", "add", "-b", branch, str(wt_path)]

        result = subprocess.run(cmd, cwd=self.project_root, capture_output=True, text=True)
        if result.returncode != 0:
            raise WorktreeError(
                f"worktree add 失败: {result.stderr.strip() or result.stdout.strip()}"
            )
        return wt_path

    def rebuild_worktree(self, task_id: str) -> Path:
        """失败 attempt 后彻底擦干净 worktree——删除并从主分支重建。"""
        wt_path = self.worktrees_dir / task_id
        branch = f"task/{task_id}"

        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=self.project_root, capture_output=True,
            )
        # 删除可能残留的分支以重置历史
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=self.project_root, capture_output=True,
        )
        return self.ensure_worktree(task_id)

    @staticmethod
    def head_sha(worktree: Path) -> str:
        """获取 worktree 当前 HEAD SHA，作为失败回滚锚点。"""
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree, capture_output=True, text=True, check=True,
        )
        return r.stdout.strip()

    def merge_into_main(self, task_id: str) -> bool:
        """成功路径：把 task 分支的成果合到主分支。

        简化策略：fast-forward only。如果有冲突就让 PM 在下一轮手动 replan。
        返回 True/False 表示是否成功合并。
        """
        branch = f"task/{task_id}"
        # 切到 main
        r = subprocess.run(
            ["git", "checkout", "main"],
            cwd=self.project_root, capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False
        r = subprocess.run(
            ["git", "merge", "--no-edit", branch],
            cwd=self.project_root, capture_output=True, text=True,
        )
        return r.returncode == 0

    def remove(self, task_id: str) -> None:
        """删除 worktree（任务彻底完成或被放弃后调用）。"""
        wt_path = self.worktrees_dir / task_id
        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=self.project_root, capture_output=True,
            )
