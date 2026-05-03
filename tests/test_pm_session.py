"""PMSessionManager 状态机回归测试。

3 个测试：
13. PM 正常决策不触发重试
14. PM 决策超时触发 session 重启 + memory recovery
15. 连续 N 次超时抛 PMSessionMaxRetriesError
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from conductor.pm_session import (
    PMSessionManager,
    PMSessionMaxRetriesError,
    PM_DECISION_TIMEOUT_SEC,
)


def _mock_project():
    """构造一个 PMSessionManager 需要的最小 project mock。"""
    project = MagicMock()
    # memory.load 路径
    project.memory_md = MagicMock()
    project.guardrails_md = MagicMock()
    project.guardrails_md.read_text.return_value = "# guardrails"
    project.project_md = MagicMock()
    project.project_md.read_text.return_value = "# project"
    return project


# ---------- 测试 13：正常决策不重试 ----------

def test_pm_normal_decision_no_retry():
    """正常返回 JSON 时，retry_count 保持 0。"""
    project = _mock_project()
    mgr = PMSessionManager(project, ["echo", "hi"])

    # mock 内部 send_recv 立即返回
    mgr._send_and_recv = MagicMock(return_value={"action": "complete"})
    mgr.start_session = MagicMock()
    mgr.session = MagicMock()
    mgr.session.poll.return_value = None  # 假装 session 活着

    decision = mgr.decide("test prompt")
    assert decision["action"] == "complete"
    assert mgr.retry_count == 0
    mgr.shutdown()


# ---------- 测试 14：超时触发 session 重启 + memory 注入 ----------

def test_pm_timeout_triggers_session_restart_with_memory():
    """第一次决策卡住超时 → 等 5min（mock 加速到 0.1s） → 重启 session（带 recovery） → 第二次成功。"""
    project = _mock_project()

    # patch memory.load 别真去读文件
    with patch("conductor.memory.StructuredMemory.load") as mock_load:
        mock_mem = MagicMock()
        mock_mem.to_markdown.return_value = "# Memory Content"
        mock_load.return_value = mock_mem

        # 改小 timeout 和 retry interval 加速测试
        mgr = PMSessionManager(
            project,
            ["echo", "hi"],
            decision_timeout_sec=1,  # 1 秒就超时
            retry_interval_sec=0.1,
            max_retries=3,
        )

        # mock 第一次 hang，第二次成功
        call_count = [0]

        def mock_send(prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                time.sleep(2)  # 超过 1 秒就触发 timeout
                return {"action": "stuck"}  # 不应该返回到这里
            return {"action": "complete"}

        mgr._send_and_recv = mock_send
        mgr.start_session = MagicMock()
        mgr.session = MagicMock()
        mgr.session.poll.return_value = None

        decision = mgr.decide("test")
        assert decision["action"] == "complete"
        assert mgr.retry_count == 0  # 成功后清零
        # 验证 start_session 至少被调用 1 次（recovery）
        # （初始 start_session 是 lazy，session.poll() 返回 None 表示活，所以可能不调初始）
        assert mgr.start_session.call_count >= 1
        # 验证最后一次 start_session 是 with_memory_recovery=True
        last_call = mgr.start_session.call_args_list[-1]
        assert last_call.kwargs.get("with_memory_recovery") is True

        mgr.shutdown()


# ---------- 测试 15：连续 N 次超时 → PMSessionMaxRetriesError ----------

def test_pm_max_retries_raises():
    """所有重试都超时 → 抛 PMSessionMaxRetriesError 让 PMAgent 转 escalate。"""
    project = _mock_project()

    with patch("conductor.memory.StructuredMemory.load") as mock_load:
        mock_load.return_value = MagicMock(to_markdown=lambda: "")

        mgr = PMSessionManager(
            project,
            ["echo"],
            decision_timeout_sec=0.5,  # 0.5 秒就超时
            retry_interval_sec=0.1,
            max_retries=2,  # 最多 2 次重试
        )

        def always_timeout(prompt):
            time.sleep(2)  # 永远超时
            return {}

        mgr._send_and_recv = always_timeout
        mgr.start_session = MagicMock()
        mgr.session = MagicMock()
        mgr.session.poll.return_value = None

        with pytest.raises(PMSessionMaxRetriesError):
            mgr.decide("test")

        mgr.shutdown()
