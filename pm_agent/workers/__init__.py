"""Worker 体系：抽象基类 + 注册表 + 内置实现。"""
from pm_agent.workers.base import WorkerDispatcher, WorkerResult
from pm_agent.workers.registry import WorkerRegistry

__all__ = ["WorkerDispatcher", "WorkerResult", "WorkerRegistry"]
