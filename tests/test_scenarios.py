"""验证 PRD 5 个场景的关键机制都已具备代码实现。

不是真实 e2e 跑——那需要 ANTHROPIC_API_KEY + 三个 worker CLI 都装好。
本测试白盒验证每个场景的核心代码路径能正常工作。
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def scenario_1_todo_app_with_concurrency():
    """场景 1：todo app 含并发（验证 dispatch_parallel 决策路径）"""
    from pm_agent.driver import Driver
    from pm_agent.pm import Decision

    # 模拟 PM 输出 dispatch_parallel 决策
    parallel_decision = {
        "action": "dispatch_parallel",
        "batch": [
            {"task_id": "task_001_init_next", "cli": "claude_code",
             "prompt": "Initialize Next.js", "files_owned": ["src/app/**"]},
            {"task_id": "task_002_drizzle", "cli": "codex",
             "prompt": "Configure Drizzle", "files_owned": ["db/**"]},
            {"task_id": "task_003_ci", "cli": "gemini",
             "prompt": "CI/CD config", "files_owned": [".github/**"]},
        ],
    }

    sig = Driver._signature(MagicMock(), Decision.from_dict(parallel_decision))
    assert sig[0] == "dispatch_parallel"
    assert len(sig[1]) == 3
    print("  OK  场景 1: dispatch_parallel 决策签名生成正常")


def scenario_2_budget_overrun():
    """场景 2：成本失控保护与自动降级"""
    from pm_agent.cost import CostTracker

    with tempfile.TemporaryDirectory() as td:
        c = CostTracker(Path(td), budget=20.0)
        c.add("worker:t1", 14.0)  # 70%
        assert c.should_degrade()
        assert not c.warning()
        c.add("worker:t2", 2.0)  # 80%
        assert c.warning()
        c.add("worker:t3", 4.5)  # > 100%
        assert c.exceeded()
    print("  OK  场景 2: 70/80/100% 成本阈值检测")


def scenario_3_file_conflict_protection():
    """场景 3：并发文件冲突防护"""
    from pm_agent.concurrency import FileLockArbiter

    class MockProject:
        pass

    arb = FileLockArbiter(MockProject())
    # task_010 改 src/api/users/* (codex)
    assert arb.try_acquire("task_010", ["src/api/users/**"])
    # task_011 想改 src/api/* —— 与 task_010 冲突
    assert not arb.try_acquire("task_011", ["src/api/**"])
    # PM 缩小为 src/api/posts/** —— 通过
    assert arb.try_acquire("task_011_v2", ["src/api/posts/**"])
    print("  OK  场景 3: file lock 拒绝重叠 glob")


def scenario_4_custom_worker_type():
    """场景 4：自定义 Worker 类型注册与调度"""
    import yaml
    from pm_agent.workers.base import WorkerDispatcher
    from pm_agent.workers.registry import WorkerRegistry

    # 模拟用户在 ~/.pm-agent/plugins/ 下放了一个 .py
    with tempfile.TemporaryDirectory() as td:
        config_dir = Path(td)
        plugins_dir = config_dir / "plugins"
        plugins_dir.mkdir()

        (plugins_dir / "aider_worker.py").write_text("""
from pm_agent.workers.base import WorkerDispatcher

class AiderWorker(WorkerDispatcher):
    cli_name = "aider_test"

    def build_command(self, worktree):
        return ["aider", "--yes-always", "--message", "-"]

    def get_env(self, use_api_key):
        import os
        env = os.environ.copy()
        return env
""", encoding="utf-8")

        config = config_dir / "workers.yaml"
        config.write_text(yaml.safe_dump({
            "workers": {
                "aider_test": {
                    "module": "aider_worker",
                    "class": "AiderWorker",
                }
            }
        }))

        # 重置注册表（测试隔离）
        WorkerRegistry._user_loaded = False
        n = WorkerRegistry.load_user_workers(config)
        assert n == 1, f"应该加载 1 个，实际 {n}"

        worker = WorkerRegistry.get("aider_test")
        cmd = worker.build_command(Path("."))
        assert "aider" in cmd[0]
    print("  OK  场景 4: 自定义 Worker 注册并调用 build_command")


def scenario_5_knowledge_persistence():
    """场景 5：知识沉淀 —— MEMORY 段注入到 worker prompt"""
    from pm_agent.memory import StructuredMemory

    m = StructuredMemory.parse("""
# 项目宪法（永远不变）
- 技术栈：Next.js 14 + Drizzle
- 不用：Prisma、Jest

# 当前架构（随实现演进）
- 认证：NextAuth + Google OAuth

# 已知坑（worker 必读，避免重复踩）
- Tailwind 4 @apply 废弃
- Drizzle migrate 在 Windows 路径有 bug

# 当前未完成任务的上下文（只放正在做的）

# 已完成里程碑（一句话）
- 项目脚手架完成
""")

    # 注入只取相关段
    injected = m.inject_for_worker(["项目宪法", "已知坑"])
    assert "Next.js 14" in injected
    assert "Tailwind 4" in injected
    # 当前架构段不该出现（没要求注入）
    assert "NextAuth" not in injected
    # 字符压缩有效
    assert len(injected) < len(m.to_markdown())
    print("  OK  场景 5: MEMORY 段选择性注入 worker prompt")


if __name__ == "__main__":
    scenario_1_todo_app_with_concurrency()
    scenario_2_budget_overrun()
    scenario_3_file_conflict_protection()
    scenario_4_custom_worker_type()
    scenario_5_knowledge_persistence()
    print()
    print("[OK] 5 个 PRD 场景的核心机制白盒测试通过")
