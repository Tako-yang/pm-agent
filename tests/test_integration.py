"""端到端集成测试：构造一个最小 project，跑通 init → escalation → reply 流程。

不需要真实 ANTHROPIC_API_KEY——所有 PM 调用都用 mock。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_escalation_lifecycle():
    from conductor.escalation import EscalationStore

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        store = EscalationStore(proj)

        # 创建 escalation
        path = store.create("Test escalation", "测试内容")
        assert path.exists()

        # list_pending
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

        # reply
        n = store.reply_latest("approved")
        assert n == 1

        # 状态改为 replied
        pending = store.list_pending()
        assert len(pending) == 0
        all_e = store.list_all()
        assert all_e[0]["status"] == "replied"

        # create_once 同 key 只创建一次
        store.create_once("budget_80", "Budget warning", "80%")
        store.create_once("budget_80", "Budget warning", "80%")
        all_e = store.list_all()
        # 应该只有 2 条（test + budget_80）
        assert len(all_e) == 2

        print("  OK  escalation lifecycle")


def test_corrections_double_confirm():
    from conductor.corrections import MemoryCorrectionStore
    from conductor.project_store import ProjectStore

    with tempfile.TemporaryDirectory() as td:
        project = ProjectStore(Path(td) / "demo")
        store = MemoryCorrectionStore(project)

        # 加入待验证项
        cid = store.add(
            {"section": "已知坑", "action": "remove", "content": "Tailwind"},
            source_task_id="task_001",
        )

        # 待注入：尚无验证 → 应在列表里
        pending = store.get_pending_for_injection()
        assert len(pending) == 1

        # 第二个 worker 反对
        store.add_verification(cid, "task_002", agreed=False)

        # 取确认的：还没人同意 → 0
        confirmed = store.take_confirmed()
        assert len(confirmed) == 0

        # 第三个 worker 同意
        store.add_verification(cid, "task_003", agreed=True)
        confirmed = store.take_confirmed()
        assert len(confirmed) == 1

        # 再次取：已 resolved，不再返回
        assert store.take_confirmed() == []
        print("  OK  corrections double-confirm")


def test_cost_tracker():
    from conductor.cost import CostTracker

    with tempfile.TemporaryDirectory() as td:
        c = CostTracker(Path(td), budget=10.0)
        c.add("worker:t1", 3.0)
        c.add("worker:t2", 4.0)
        c.add("pm:iter_5", 0.1)

        assert abs(c.total() - 7.1) < 0.001
        assert c.ratio() == 0.71
        assert c.should_degrade()  # 70%+
        assert not c.warning()  # < 80%
        assert not c.exceeded()  # < 100%

        c.add("worker:t3", 1.0)
        assert c.warning()  # > 80%

        c.add("worker:t4", 5.0)
        assert c.exceeded()

        # 持久化
        c2 = CostTracker(Path(td))
        assert abs(c2.total() - 13.1) < 0.001
        assert c2.budget == 10.0
        print("  OK  cost tracker persistence + thresholds")


def test_task_store_runnable_filter():
    from conductor.tasks import Task, TaskStore

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "TASKS.json"
        store = TaskStore(path, project_id="demo")
        # 三个任务：t1 done, t2 依赖 t1, t3 依赖 t2
        store.upsert(Task(id="task_001_a", title="a", status="done"))
        store.upsert(Task(id="task_002_b", title="b", status="pending", depends_on=["task_001_a"]))
        store.upsert(Task(id="task_003_c", title="c", status="pending", depends_on=["task_002_b"]))

        runnable = store.runnable()
        # 只有 t2 应该可跑（依赖满足）
        assert len(runnable) == 1
        assert runnable[0].id == "task_002_b"
        print("  OK  task store runnable filter")


def test_memory_distillation_threshold():
    from conductor.memory import StructuredMemory

    m = StructuredMemory({s: "" for s in StructuredMemory.SECTIONS})
    assert not m.needs_distillation()

    # 塞满 3000+ 字
    m.sections["已知坑（worker 必读，避免重复踩）"] = "x" * 3500
    assert m.needs_distillation()
    print("  OK  memory distillation threshold")


def test_pm_decision_signature():
    from conductor.driver import Driver
    from conductor.pm import Decision

    # 同 task 不同 cli → 视为相似（原地打转）
    a = Decision.from_dict({"action": "dispatch", "task_id": "task_001", "cli": "codex"})
    b = Decision.from_dict({"action": "dispatch", "task_id": "task_001", "cli": "claude_code"})
    sig_a = (a.action, a.raw["task_id"], a.raw["cli"])
    sig_b = (b.action, b.raw["task_id"], b.raw["cli"])
    assert Driver._is_similar_decision(sig_a, sig_b)

    # 不同 task → 不相似
    c = Decision.from_dict({"action": "dispatch", "task_id": "task_002", "cli": "codex"})
    sig_c = (c.action, c.raw["task_id"], c.raw["cli"])
    assert not Driver._is_similar_decision(sig_a, sig_c)
    print("  OK  pm decision signature similarity")


def test_full_init_with_mock_pm():
    """模拟一次 conductor init：用 mock client 让 PM 返回固定的三件套。"""
    from conductor.project_init import init_project

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='''
```project_md
# 项目宪法

技术栈: Next.js 14 + PostgreSQL
```

````guardrails_md
# 项目护栏

## 技术栈红线

```yaml
# rules: tech_stack
forbidden_dependencies:
  - prisma
required_stack:
  framework: next.js
```

## 范围红线

```yaml
# rules: scope
out_of_scope:
  - payment
```

## 安全红线

```yaml
# rules: security
forbidden_patterns:
  - "API_KEY"
```

## 决策红线

```yaml
# rules: must_escalate
must_escalate:
  - introduce_new_framework
```
````

```tasks_json
{
  "project_id": "demo",
  "version": 1,
  "tasks": [
    {
      "id": "task_001_setup",
      "title": "Setup Next.js scaffold",
      "status": "pending",
      "description": "Initialize project",
      "acceptance_criteria": "package.json exists",
      "files_owned": ["package.json", "next.config.js"]
    }
  ]
}
```
''')]
    mock_client.messages.create.return_value = mock_response

    with tempfile.TemporaryDirectory() as td:
        projects_root = Path(td)
        project_path = init_project(
            project_id="demo",
            requirement="测试需求",
            budget=10.0,
            projects_root=projects_root,
            pm_client=mock_client,
        )

        # 校验产物
        assert (project_path / "PROJECT.md").exists()
        assert (project_path / "GUARDRAILS.md").exists()
        assert (project_path / "TASKS.json").exists()
        assert (project_path / "MEMORY.md").exists()
        assert (project_path / ".git").exists()

        # GUARDRAILS.md 应该可解析
        from conductor.guardrails import GuardrailsChecker
        errors = GuardrailsChecker.validate_file(project_path / "GUARDRAILS.md")
        assert errors == [], f"GUARDRAILS.md 应可解析: {errors}"

        # 应该创建了一条 escalation
        from conductor.escalation import EscalationStore
        pending = EscalationStore(project_path).list_pending()
        assert len(pending) == 1
        assert "确认" in pending[0]["title"]

        print("  OK  full init flow with mocked PM")


if __name__ == "__main__":
    test_escalation_lifecycle()
    test_corrections_double_confirm()
    test_cost_tracker()
    test_task_store_runnable_filter()
    test_memory_distillation_threshold()
    test_pm_decision_signature()
    test_full_init_with_mock_pm()
    print("\n[OK] 所有集成测试通过")
