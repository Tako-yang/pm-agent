# 自定义 Worker 开发指南

> Conductor 内置 3 种 Worker：`claude_code` / `codex` / `gemini`。
> 想用其它 CLI（Aider / OpenCode / 本地模型 / 你自己的脚本）？50 行代码搞定。

## 1. 基本概念

每个 Worker 类型是一个继承 `WorkerDispatcher` 的 Python 类，必须实现 2 个方法：

| 方法 | 用途 |
|---|---|
| `build_command(worktree)` | 构造 subprocess 命令（数组形式） |
| `get_env(use_api_key)` | 构造环境变量（决定走 API key 还是订阅认证） |

可选重写：

| 方法 | 默认行为 | 何时重写 |
|---|---|---|
| `parse_output(stdout)` | 抓 `<FEEDBACK>` 块 | CLI 输出非标准格式时 |
| `estimate_cost(stdout, stderr)` | 返回 0.0 | 想追踪 API 成本时 |

`dispatch()` 方法已封装好进程组管理 / 超时 / 失败回滚 / FEEDBACK 解析等基础设施，**不要重写**。

## 2. 完整示例：Aider Worker

`~/.conductor/plugins/aider_worker.py`：

```python
import os
import re
from pathlib import Path

from conductor.workers.base import WorkerDispatcher


class AiderWorker(WorkerDispatcher):
    """Aider CLI 的 Worker 适配器。

    Aider 通过 stdin 接收消息，--yes-always 跳过所有交互确认。
    """

    cli_name = "aider"  # 必填：唯一标识符

    def build_command(self, worktree: Path) -> list[str]:
        return [
            "aider",
            "--yes-always",
            "--no-show-model-warnings",
            "--message", "-",  # 从 stdin 读
        ]

    def get_env(self, use_api_key: bool) -> dict:
        env = os.environ.copy()
        if not use_api_key:
            # 订阅模式：清掉 API key 让 CLI 走自己的认证
            env.pop("OPENAI_API_KEY", None)
        return env

    def estimate_cost(self, stdout: str, stderr: str) -> float:
        """Aider 在 stderr 末尾打印 'Tokens: ... Cost: $X.XX'。"""
        match = re.search(r"Cost:\s*\$([0-9.]+)", stderr or "")
        return float(match.group(1)) if match else 0.0
```

## 3. 注册到 Conductor

`~/.conductor/workers.yaml`：

```yaml
workers:
  aider:
    module: aider_worker  # 对应 plugins/aider_worker.py
    class: AiderWorker
```

## 4. 验证

```bash
$ conductor workers list
内置 Worker:
  claude_code
  codex
  gemini
自定义 Worker:
  aider

$ conductor workers test aider
[OK] aider 二进制可调用 (/usr/local/bin/aider)
[OK] 注册类继承 WorkerDispatcher
[OK] build_command 返回: ['aider', '--yes-always', ...]
```

## 5. PM 如何选用你的 Worker

PM 在每轮决策时通过 `WorkerRegistry.list_all()` 看到所有可用类型。它会根据 `assigned_cli` 字段派发任务：

```json
{
  "action": "dispatch",
  "task_id": "task_007_oauth",
  "cli": "aider",
  "prompt": "...",
  "files_owned": ["src/auth/**"]
}
```

要让 PM 倾向于在某些场景用你的 Worker，可以在 `PROJECT.md` 里加提示，例如："对于 Python 重构任务优先用 aider"。

## 6. 加载机制详解

`WorkerRegistry.load_user_workers()` 在以下时机被调用：

- `conductor workers list/test` 命令
- `Driver.__init__` 启动时（一次性）

它会：
1. 读 `~/.conductor/workers.yaml`
2. 对每个 `workers.<name>` 条目：
   - 优先从 `~/.conductor/plugins/<module>.py` 直接加载（不走 sys.path）
   - 找不到则尝试 `import <module>`（适合用户已 pip install 自己的包）
3. 注册类到 `WorkerRegistry._registry`

## 7. 高级技巧

### 7.1 自定义 FEEDBACK 解析

如果你的 CLI 不输出 `<FEEDBACK>` 块（比如它只输出 diff），可以重写 `parse_output`：

```python
def parse_output(self, stdout: str) -> Optional[dict]:
    # 自己构造一个 FEEDBACK dict
    return {
        "task_id": "?",  # 上层会从派发时的 task_id 补全
        "status": "completed" if "Successfully" in stdout else "failed",
        "summary": stdout[-200:],
        "files_changed": [],
        "memory_updates": [],
        "memory_corrections": [],
        "blockers": [],
    }
```

### 7.2 注入额外环境变量

```python
def get_env(self, use_api_key: bool) -> dict:
    env = os.environ.copy()
    env["MY_TOOL_MODEL"] = "gpt-4o"
    env["MY_TOOL_TIMEOUT"] = "1200"
    if use_api_key:
        env["OPENAI_API_KEY"] = os.environ["WORKER_OPENAI_API_KEY"]
    else:
        env.pop("OPENAI_API_KEY", None)
    return env
```

### 7.3 使用工作目录

`worktree` 参数是 driver 给 task 准备的独立 git worktree（在 `<project>/worktrees/<task_id>/`）。Worker 默认 cwd 已经被设为这个目录，所以你的命令通常不需要 `--cd <path>`。

但如果你的 CLI 需要显式工作目录参数，可以这样写：

```python
def build_command(self, worktree: Path) -> list[str]:
    return ["my_tool", "--workdir", str(worktree), "..."]
```

## 8. 注意事项

- `cli_name` 必须唯一，不要和内置 3 种重名（除非你想替换内置实现）
- 不要在 `build_command` 里读环境变量——用 `get_env`，否则 dispatch 时不会看到改动
- `dispatch()` 已经封装了"派发前 git SHA 快照 + 失败 reset --hard 回滚"，你的 Worker 不需要管这些
- Worker 的 stdin 接收的是 PM 写的完整 prompt（包含 ROLE / MEMORY / TASK / CONSTRAINTS / OUTPUT 五段）

## 9. 调试

观察 worker 实际跑的内容：

```bash
$ conductor logs <project_id> --task task_007
```

或看失败归档：

```bash
$ ls projects/<project_id>/logs/task_007_<timestamp>_failed/
git_diff.patch    # worker 改了什么（已被 reset 回滚）
git_log.txt       # worker 期间的 commits
untracked.txt     # 未追踪文件
stderr_tail.txt   # stderr 末 200 行
reason.txt        # 失败原因
```
