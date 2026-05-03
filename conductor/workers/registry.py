"""WorkerRegistry：Worker 类型注册表。

管理：
- 内置 Worker（claude_code / codex / gemini）：模块导入时自动注册
- 用户自定义 Worker：从 ~/.conductor/workers.yaml 加载

加载 user workers 的时机：
- CLI 命令 `conductor workers list/test` 显式调用
- driver 启动时（一次性）
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from conductor.workers.base import WorkerDispatcher


class WorkerRegistry:
    _registry: dict[str, type[WorkerDispatcher]] = {}
    _user_loaded: bool = False

    @classmethod
    def register(cls, dispatcher_cls: type[WorkerDispatcher]) -> None:
        if not getattr(dispatcher_cls, "cli_name", ""):
            raise ValueError(f"{dispatcher_cls.__name__} 必须设置 cli_name")
        cls._registry[dispatcher_cls.cli_name] = dispatcher_cls

    @classmethod
    def get(cls, cli_name: str) -> WorkerDispatcher:
        if cli_name not in cls._registry:
            raise ValueError(
                f"未注册的 Worker 类型: {cli_name}。"
                f"已注册: {', '.join(sorted(cls._registry.keys()))}"
            )
        return cls._registry[cli_name]()

    @classmethod
    def list_all(cls) -> list[str]:
        return sorted(cls._registry.keys())

    @classmethod
    def load_user_workers(cls, config_path: Optional[Path] = None) -> int:
        """从 ~/.conductor/workers.yaml 加载用户自定义 Worker。

        config_path 可显式指定（测试用）；默认从 $HOME/.conductor/workers.yaml。
        允许从两种位置 import 用户类：
        - 已安装的 Python 包（spec.module 是模块路径）
        - ~/.conductor/plugins/ 目录下的散文件（按 module 名匹配 .py）

        返回加载的 worker 数量。
        """
        if cls._user_loaded and config_path is None:
            return 0  # 同一进程只加载一次（避免重复 import 副作用）

        config_path = config_path or Path.home() / ".conductor" / "workers.yaml"
        if not config_path.exists():
            cls._user_loaded = True
            return 0

        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise RuntimeError(f"解析 {config_path} 失败: {e}") from e

        plugins_dir = config_path.parent / "plugins"
        n = 0
        for name, spec in (config.get("workers") or {}).items():
            module_name = spec.get("module")
            class_name = spec.get("class")
            if not module_name or not class_name:
                continue

            # 优先从 plugins 目录加载 .py 散文件
            plugin_file = plugins_dir / f"{module_name}.py"
            module = None
            if plugin_file.exists():
                module = cls._load_module_from_file(module_name, plugin_file)
            else:
                # 退回标准 import（用户已 pip install 自己的包）
                try:
                    module = importlib.import_module(module_name)
                except ImportError:
                    continue

            klass = getattr(module, class_name, None)
            if klass is None or not issubclass(klass, WorkerDispatcher):
                continue
            # 用户配置中的 name 覆盖类的 cli_name（允许 alias）
            if not getattr(klass, "cli_name", ""):
                klass.cli_name = name
            cls.register(klass)
            n += 1

        cls._user_loaded = True
        return n

    @staticmethod
    def _load_module_from_file(name: str, path: Path):
        """从一个 .py 文件加载模块——不通过 sys.path。"""
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


# ---------- 内置 Worker 自动注册 ----------

# 在最底部 import 避免循环依赖
from conductor.workers.claude_code import ClaudeCodeWorker  # noqa: E402
from conductor.workers.codex import CodexWorker  # noqa: E402
from conductor.workers.gemini import GeminiWorker  # noqa: E402

WorkerRegistry.register(ClaudeCodeWorker)
WorkerRegistry.register(CodexWorker)
WorkerRegistry.register(GeminiWorker)
