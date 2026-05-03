# pm-agent

> Octopus Assistant · 章鱼助手 — nickname for the design (see below).
>
> [English](#english) · [中文](#中文)

<a name="english"></a>

A PM agent that supervises your existing coding-CLI subscriptions (Claude Code, Codex, Gemini) so you can hand it a one-sentence requirement and walk away. No API keys. Reuses subscriptions you already pay for.

[![Status](https://img.shields.io/badge/status-early%20development-orange)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)]()
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Per-project API cost](https://img.shields.io/badge/per--project%20API%20cost-%240-brightgreen)]()

Status: early development. Architecture mostly settled, MVP in progress. Not production-ready.

---

## The problem

Say you want a Next.js todo app with Google OAuth.

You open Claude Code. Type the request. It scaffolds, then asks "App Router or Pages?" You answer. It writes routes, then asks "Tailwind or vanilla CSS?" You answer. Twenty minutes in: "NextAuth v4 or v5?" You answer.

You're stuck at the screen, answering one decision every few minutes. The frontend waits while you think about the backend. The backend waits while you decide on the ORM. Four hours later you have something half-built.

That's the single-CLI co-pilot pattern. You're still the project manager.

## What pm-agent does instead

You hand pm-agent the same one-sentence requirement.

It drafts `PROJECT.md` (constitution) and `GUARDRAILS.md` (red lines). Asks you once: does this look right? You glance over the 23 tasks, sensible stack, scope is clear. You reply `approved, but use Drizzle not Prisma` and leave.

pm-agent dispatches three workers in parallel:

- Worker 1 (Claude Code) on the frontend
- Worker 2 (Codex) on the backend API
- Worker 3 (Gemini) on CI/CD

It tracks progress, decides what to dispatch next when one finishes, retries failures (sometimes by switching CLI types), and only interrupts you for real boundary calls — architecture forks, scope questions, budget warnings.

You come back later. The project is built and tested. You spent maybe 30 minutes total.

## Why not [other multi-agent tool]

There are plenty: MetaGPT, OpenDevin, AutoGen, Aider's multi-file mode. Most of them require API keys. Every PM decision and every worker action gets billed per token. A serious project (not a toy) easily burns $50–$300 in API costs.

pm-agent uses your existing CLI subscriptions instead.

| Approach | Per-project cost |
|---|---|
| Multi-agent driven by Opus / GPT-5 API directly | $80 – $300 |
| Most existing multi-agent OSS (require API keys) | $30 – $150 |
| pm-agent + your CLI subscriptions | $0 marginal API cost |

The mechanism: pm-agent treats your already-paid Claude Code Pro/Max, OpenAI Plus (Codex), and Google AI Pro (Gemini) subscriptions as the worker pool. The PM itself runs as a long-session Claude Code subscription — so PM decisions don't cost API either.

If you already use one of these CLIs day-to-day, pm-agent is a near-free upgrade. You need at least one subscription (Claude Pro/Max recommended). Two or three for real parallelism.

---

## Quick start

Prerequisites: Python 3.11+, git 2.30+, and at least one of:

- [Claude Code CLI](https://docs.anthropic.com/claude-code) (Pro or Max)
- [Codex CLI](https://github.com/openai/codex) (OpenAI Plus or Pro)
- [Gemini CLI](https://github.com/google/generative-ai-cli) (Google AI Pro)

Windows: WSL2 strongly recommended.

Install:

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e .

pm-agent --version
pm-agent workers list
```

Make sure your CLIs are logged in:

```bash
claude /login
codex login
gemini auth login
```

First project:

```bash
pm-agent init my-todo-app \
    --requirement "A Next.js todo app with Google OAuth and PostgreSQL" \
    --budget 30 \
    --max-concurrent 3

cat projects/my-todo-app/PROJECT.md
cat projects/my-todo-app/GUARDRAILS.md
pm-agent reply my-todo-app "approved, but use Drizzle not Prisma"

pm-agent watch my-todo-app
```

---

## What it does

- Multi-CLI orchestration. Mix Claude Code, Codex, Gemini, or any CLI you wrap in a 50-line plugin.
- True parallelism. Up to N workers running simultaneously, with file-lock arbitration to prevent conflicts.
- Persistent project memory. A 5-section `MEMORY.md` that auto-distills as it grows.
- Project guardrails. `GUARDRAILS.md` defines what PM may not decide alone (frameworks, scope, security).
- Triple-layer cost control. Per-call token caps, per-task budget, project budget. Concurrency auto-degrades at 70% spend.
- Worker isolation. Each task runs in its own `git worktree`. Failures auto-cleanup with snapshot archival.
- Self-healing. Stuck-loop detection, auto-retry with worker-type switching, orphan process cleanup on crash recovery.
- CLI-only. `pm-agent watch <project>` for live status. No web UI, no daemon.
- Transparent. Every PM decision logged to `decisions.log`.

## What it doesn't do

It won't replace your judgment on big architecture calls; PM escalates those to you. It won't scale to massive monorepos (>100k LOC) — worker context windows limit task size. Pro tier alone may not be enough; long PM sessions can hit Pro quota, so Max is recommended. Bad spec in, bad project out — pm-agent doesn't fix unclear requirements. Native Windows works but is best-effort; WSL2 is the supported path.

---

## How it works

Three roles:

```
Boss (you)
    Approves PROJECT.md and GUARDRAILS.md once.
    Responds to occasional escalations.
        |
        v
Driver Loop (Python process)
    Owns control flow. Enforces guardrails, manages workers,
    tracks cost, recovers from crashes.
        |
        v
PM Agent (long Claude Code session)
    Decomposes tasks, picks workers, writes worker prompts.
    Outputs JSON decisions.
        |
        v
Worker Pool (default N=3)
    [ CC ] [ Codex ] [ Gemini ]
    Each in its own git worktree. One-shot subprocess per task.
```

The "octopus" name comes from this shape: a central brain (PM) coordinates several arms (workers) running tasks in parallel, each in its own isolated workspace.

A few design choices worth knowing:

The driver is a Python loop, not an LLM. Control flow stays deterministic and inspectable. The PM only outputs decisions; the driver decides whether to keep going.

Workers are one-shot subprocesses. No shared context. No memory leaks across tasks. Each starts fresh, runs one task, dies.

Each worker gets its own git worktree. File-system-level isolation lets two workers edit different parts of the codebase simultaneously without git conflicts.

State is entirely on disk: `MEMORY.md`, `TASKS.json`, `decisions.log`. Crash mid-project, run `pm-agent resume`, continue.

---

## Technical details

For the curious. Skip if you just want to use it.

### Cross-platform process management

Worker subprocesses can spawn `npm`, `pnpm`, dev servers. Killing the parent isn't enough. pm-agent uses kernel-level primitives:

| Platform | Primary | Fallback |
|---|---|---|
| Windows | Job Object + `KILL_ON_JOB_CLOSE` | psutil residue scan |
| POSIX | `setsid()` + `killpg(SIGKILL)` | psutil residue scan |

`psutil` is auxiliary (orphan scan, I/O activity tracking). Kill guarantees come from OS primitives, with no race window.

### Concurrent dispatch

```python
while not project_done():
    decision = pm.decide_once()
    if decision.action == "dispatch_parallel":
        for task in decision.batch:
            pool.submit(task)
        task_id, result = pool.wait_any()  # OS-level event, <10ms
        on_worker_complete(task_id, result)
```

Built on `concurrent.futures` with kernel condition variables. No polling.

### Failure recovery

Any non-success path triggers cleanup:

```
Worker fails -> archive worktree state (diff, new files, stderr tail)
             -> git reset --hard <pre_dispatch_sha>
             -> git clean -fd
             -> mark dirty, force rebuild before retry
```

PM gets failure context in its next decision and can switch worker type or escalate.

### Driver crash recovery

Workers tag themselves with environment variables (`PM_AGENT_PROJECT_ID`, `PM_AGENT_DRIVER_PID`). On restart, the driver scans for orphans whose owning driver is dead and kills them. Resume with `pm-agent resume <project>`.

---

## CLI reference

```
pm-agent init <id> --requirement "..." [--budget 50] [--max-concurrent 3]
pm-agent start <id>
pm-agent pause / resume / stop <id>

pm-agent status <id>
pm-agent watch <id>
pm-agent list
pm-agent logs <id> [--task task_007] [--tail 50]
pm-agent decisions <id>

pm-agent reply <id> "your response"
pm-agent escalations <id>

pm-agent guardrails <id> [--edit] [--validate]

pm-agent workers list
pm-agent workers test <name>

pm-agent inspect <id> --task task_007
pm-agent memory <id> [--history]
pm-agent cost <id>
pm-agent pool <id>
```

---

## Custom workers

About 50 lines for a new CLI:

```python
# ~/.pm-agent/plugins/aider_worker.py
from pm_agent.workers.base import WorkerDispatcher

class AiderWorker(WorkerDispatcher):
    cli_name = "aider"

    def build_command(self, worktree):
        return ["aider", "--yes-always", "--message", "-"]

    def get_env(self, use_api_key):
        env = os.environ.copy()
        if not use_api_key:
            env.pop("OPENAI_API_KEY", None)
        return env
```

Register in `~/.pm-agent/workers.yaml`:

```yaml
workers:
  aider:
    module: aider_worker
    class: AiderWorker
```

Verify: `pm-agent workers test aider`. Full guide: [`docs/CUSTOM_WORKERS.md`](docs/CUSTOM_WORKERS.md).

---

## Contributing

Early-stage. Big changes welcome but open an issue first.

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e ".[dev]"
pytest
ruff check . && mypy pm_agent
```

## License

[MIT](LICENSE).

---

<a name="中文"></a>

# pm-agent（中文）

> Octopus Assistant · 章鱼助手 — 设计的昵称（见下文）。
>
> [English](#english) · [中文](#中文)

一个 PM agent，监督你已经在用的编码 CLI（Claude Code、Codex、Gemini）。给它一句话需求，你就可以走开。不要 API key，复用你已经付过的订阅。

[![状态](https://img.shields.io/badge/status-早期开发-orange)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)]()
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![单项目 API 成本](https://img.shields.io/badge/单项目%20API%20成本-%240-brightgreen)]()

状态：早期开发。架构基本定型，MVP 实现进行中。不是生产就绪。

---

## 它解决什么问题

你想做一个 Next.js todo app，要 Google OAuth。

你打开 Claude Code，输入这个需求。它搭了点初始代码，问 "App Router 还是 Pages Router？" 你回。它写了几个路由，又问 "Tailwind 还是 vanilla CSS？" 你回。20 分钟后又问 "NextAuth v4 还是 v5？" 你回。

你被钉在屏幕前，每几分钟回答一次决策。前端等你想后端，后端等你定 ORM。4 小时刷新过去，你只搞出一个半成品。

这是单 CLI 的"副驾"模式。你还是项目经理。

## pm-agent 怎么改这个

你给 pm-agent 同样的一句话需求。

它起草 `PROJECT.md`（项目宪法）和 `GUARDRAILS.md`（红线）。问你一次：这个计划行不行？你瞄一眼：23 个任务、技术栈合理、范围清楚。你回 `approved，但用 Drizzle 不要 Prisma`，然后离开。

pm-agent 并发派出三个 worker：

- Worker 1（Claude Code）做前端
- Worker 2（Codex）做后端 API
- Worker 3（Gemini）做 CI/CD

它跟踪进度，谁完成了就决定下一步派什么，失败时重试或换 CLI 类型，只在撞到真实边界时找你 — 架构岔路、超出范围、预算告警。

你过会儿回来，项目搭完了，测试通过。你总共投入大约 30 分钟。

## 为什么不用别的 multi-agent 工具

这领域已经有不少：MetaGPT、OpenDevin、AutoGen、Aider 多文件模式等。它们大多需要 API key，每次 PM 决策、每个 worker 调用都按 token 计费。一个认真的项目（不是玩具）轻松烧 $50–$300。

pm-agent 用你已有的 CLI 订阅代替。

| 方案 | 单项目成本 |
|---|---|
| 用 Opus / GPT-5 API 直接驱动 multi-agent | $80 – $300 |
| 大多数现有 multi-agent 开源项目（要 API key） | $30 – $150 |
| pm-agent + 你已有的 CLI 订阅 | $0 边际 API 成本 |

机制：pm-agent 把你已经付钱的 Claude Code Pro/Max、OpenAI Plus（Codex）、Google AI Pro（Gemini）订阅当 worker pool。PM 自己也跑在 Claude Code 长 session 上，所以 PM 决策也不走 API 计费。

如果你已经在日常用其中一个 CLI，pm-agent 是个近乎免费的升级。至少需要一个订阅（推荐 Claude Pro/Max）。两三家才能真并发。

---

## 快速开始

前置条件：Python 3.11+、git 2.30+，以及至少一个：

- [Claude Code CLI](https://docs.anthropic.com/claude-code)（Pro 或 Max）
- [Codex CLI](https://github.com/openai/codex)（OpenAI Plus 或 Pro）
- [Gemini CLI](https://github.com/google/generative-ai-cli)（Google AI Pro）

Windows 用户：强烈推荐 WSL2。

安装：

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e .

pm-agent --version
pm-agent workers list
```

确认你的 CLI 已登录：

```bash
claude /login
codex login
gemini auth login
```

第一个项目：

```bash
pm-agent init my-todo-app \
    --requirement "用 Next.js + Google OAuth + PostgreSQL 做一个 todo app" \
    --budget 30 \
    --max-concurrent 3

cat projects/my-todo-app/PROJECT.md
cat projects/my-todo-app/GUARDRAILS.md
pm-agent reply my-todo-app "approved，但用 Drizzle 不要 Prisma"

pm-agent watch my-todo-app
```

---

## 它能做什么

- 多 CLI 编排。混搭 Claude Code、Codex、Gemini，或写 50 行插件接入任意 CLI。
- 真并发。最多 N 个 worker 同时跑，文件锁仲裁防冲突。
- 持久化项目记忆。5 段 `MEMORY.md` 自动蒸馏。
- 项目护栏。`GUARDRAILS.md` 定义 PM 不可独自决策的事项（框架、范围、安全）。
- 三层成本控制。单调用 token 上限、单任务预算、项目预算。70% 自动降并发。
- Worker 隔离。每个任务独立 `git worktree`，失败自动清理 + 快照归档。
- 自愈。卡死检测、自动切 worker 类型重试、孤儿进程扫描清理。
- CLI-only。`pm-agent watch <项目>` 看实时状态，不做 Web UI，不做 daemon。
- 透明。每个 PM 决策都记到 `decisions.log`。

## 它不能做什么

不能替你做大架构决策 — PM 会 escalate。不能搞定超大 monorepo（>10 万行） — worker context 窗口限制单任务大小。Pro 单订阅可能不够 — 长 PM session 可能撞 Pro 配额，推荐 Max。垃圾需求出垃圾项目 — pm-agent 不能凭 AI 救烂 spec。原生 Windows 是 best-effort，推荐 WSL2。

---

## 它怎么工作

三个角色：

```
Boss（你）
    一次性确认 PROJECT.md 和 GUARDRAILS.md。
    偶尔回应 escalation。
        |
        v
Driver Loop（Python 进程）
    掌控循环。执行护栏、管理 worker、
    追踪成本、崩溃恢复。
        |
        v
PM Agent（长 Claude Code session）
    拆分任务、选 worker、写 worker prompt。
    输出 JSON 决策。
        |
        v
Worker Pool（默认 N=3）
    [ CC ] [ Codex ] [ Gemini ]
    各自在独立 git worktree。一次性 subprocess。
```

"章鱼"名字就是这个形状：中央大脑（PM）协调几条触手（worker）并发跑任务，各自在隔离工作区里。

几个值得了解的设计选择：

Driver 是 Python loop，不是 LLM。控制流确定可观测。PM 只输出决策，driver 决定是否继续。

Worker 是一次性 subprocess。没有共享 context。任务间不会泄漏记忆。每个 worker 起新的，跑一个任务，结束。

每个 worker 一个独立 git worktree。文件系统级隔离让两个 worker 可以同时改不同部分的代码不会 git 冲突。

状态全部落盘：`MEMORY.md`、`TASKS.json`、`decisions.log`。中途崩溃，跑 `pm-agent resume` 继续。

---

## 技术细节

给好奇的开发者。只想用的话可以跳过。

### 跨平台进程管理

Worker subprocess 会派生 `npm`、`pnpm`、dev server。杀父进程不够。pm-agent 用内核级原语：

| 平台 | 主力 | 双保险 |
|---|---|---|
| Windows | Job Object + `KILL_ON_JOB_CLOSE` | psutil 残留扫描 |
| POSIX | `setsid()` + `killpg(SIGKILL)` | psutil 残留扫描 |

`psutil` 只做辅助（孤儿扫描、I/O 活跃度跟踪）。kill 的硬保证靠 OS 原语，无 race window。

### 并发派发

```python
while not project_done():
    decision = pm.decide_once()
    if decision.action == "dispatch_parallel":
        for task in decision.batch:
            pool.submit(task)
        task_id, result = pool.wait_any()  # OS 级事件，延迟 <10ms
        on_worker_complete(task_id, result)
```

基于 `concurrent.futures` + 内核条件变量。不轮询。

### 失败恢复

任何非 success 路径都触发清理：

```
Worker 失败 -> 归档 worktree 状态（diff、新文件、stderr 末尾）
            -> git reset --hard <派发前 sha>
            -> git clean -fd
            -> 标 dirty，重派前强制重建
```

PM 在下一轮决策时拿到失败上下文，可切 worker 类型或 escalate。

### Driver 崩溃恢复

Worker 启动时打 env 标记（`PM_AGENT_PROJECT_ID`、`PM_AGENT_DRIVER_PID`）。重启时 driver 扫所属 driver 已死的孤儿 worker，kill 之。`pm-agent resume <项目>` 恢复。

---

## CLI 命令

```
pm-agent init <id> --requirement "..." [--budget 50] [--max-concurrent 3]
pm-agent start <id>
pm-agent pause / resume / stop <id>

pm-agent status <id>
pm-agent watch <id>
pm-agent list
pm-agent logs <id> [--task task_007] [--tail 50]
pm-agent decisions <id>

pm-agent reply <id> "回复内容"
pm-agent escalations <id>

pm-agent guardrails <id> [--edit] [--validate]

pm-agent workers list
pm-agent workers test <name>

pm-agent inspect <id> --task task_007
pm-agent memory <id> [--history]
pm-agent cost <id>
pm-agent pool <id>
```

---

## 自定义 Worker

约 50 行接入新 CLI：

```python
# ~/.pm-agent/plugins/aider_worker.py
from pm_agent.workers.base import WorkerDispatcher

class AiderWorker(WorkerDispatcher):
    cli_name = "aider"

    def build_command(self, worktree):
        return ["aider", "--yes-always", "--message", "-"]

    def get_env(self, use_api_key):
        env = os.environ.copy()
        if not use_api_key:
            env.pop("OPENAI_API_KEY", None)
        return env
```

注册到 `~/.pm-agent/workers.yaml`：

```yaml
workers:
  aider:
    module: aider_worker
    class: AiderWorker
```

验证：`pm-agent workers test aider`。完整指南：[`docs/CUSTOM_WORKERS.md`](docs/CUSTOM_WORKERS.md)。

---

## 贡献

早期项目。欢迎大改动，但请先开 issue。

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e ".[dev]"
pytest
ruff check . && mypy pm_agent
```

## 许可

[MIT](LICENSE)。
