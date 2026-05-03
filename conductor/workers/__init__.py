"""Worker 体系：抽象基类 + 注册表 + 内置实现。"""
from conductor.workers.base import WorkerDispatcher, WorkerResult
from conductor.workers.registry import WorkerRegistry

__all__ = ["WorkerDispatcher", "WorkerResult", "WorkerRegistry"]
