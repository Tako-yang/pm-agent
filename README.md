# pm-agent

> 🐙 **Octopus Assistant · 章鱼助手** — friendly nickname for the design metaphor below
>
> **English** | [中文](#pm-agent-中文)

**A "PM agent" that supervises your existing coding-CLI subscriptions (Claude Code, Codex, Gemini) so you can hand it a one-sentence requirement and walk away.** No API keys. No per-token billing. Reuses subscriptions you already pay for.

[![Status](https://img.shields.io/badge/status-early%20development-orange)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)]()
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Cost](https://img.shields.io/badge/per--project%20API%20cost-%240-brightgreen)]()
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20WSL2-green)]()

> ⚠️ **Status: Early development.** Architecture is largely settled, MVP implementation in progress. Not production-ready.

---

## The Problem It Solves

### How you use AI to code today

You want a Next.js todo app with Google OAuth.

You open Claude Code. Type *"build a todo app with Next.js + Google OAuth + PostgreSQL"*. It scaffolds something, then asks *"do you want App Router or Pages Router?"*. You answer. It writes some routes, then asks *"Tailwind or vanilla CSS?"*. You answer. Twenty minutes in, it asks *"NextAuth v4 or v5?"*. You answer.

You're now **pinned to your screen**, answering one decision every few minutes, watching one CLI work on one thing. The frontend waits while you think about the backend. The database waits while you decide on the ORM. After 4 hours of glorified clicking, you have something half-built.

This is the **single-CLI co-pilot** model. You're still the project manager.

### How pm-agent changes this

You hand pm-agent the same one-sentence requirement.

It drafts a `PROJECT.md` (constitution) and `GUARDRAILS.md` (red lines) and asks you **once**: "Does this look right?" You glance over: 23 tasks, sensible tech stack, scope clear. You reply *"approved, but use Drizzle not Prisma"* and **leave the room**.

pm-agent then dispatches **three workers in parallel**:
- Worker 1 (Claude Code) on the frontend
- Worker 2 (Codex) on the backend API
- Worker 3 (Gemini) on CI/CD

It tracks each worker's progress, decides what to dispatch next when one finishes, handles failures by retrying or switching CLI types, and only interrupts you when it hits a real boundary (architecture fork, scope question, budget warning).

Two hours later you come back. The project is built and tested. You spent maybe 30 minutes total — at the start (approval) and a couple of mid-project escalations.

You went from being the **driver** to being the **boss reviewing escalations**.

---

## Why Not Just Use [Another Multi-Agent Tool]?

This space already has plenty: MetaGPT, OpenDevin, AutoGen, Aider's multi-file mode, etc. Most of them have one big problem for the average developer:

**They require API keys.** Every PM decision, every worker action — billed per token to OpenAI/Anthropic. Driving a serious project (a real app, not a toy) easily burns **$50–$300 in API costs**.

pm-agent's core differentiation:

| Approach | Cost for a medium project |
|---|---|
| Multi-agent driven by Opus/GPT-5 API directly | **$80 – $300** per project |
| Most existing multi-agent OSS (require API keys) | **$30 – $150** per project |
| **pm-agent + your existing CLI subscriptions** | **$0 marginal API cost** (you already pay for the subscriptions) |

The trick: pm-agent treats your **already-paid-for** Claude Code Pro/Max, OpenAI Plus (Codex), Google AI Pro (Gemini) subscriptions as the worker pool. The PM itself runs as a long-session Claude Code subscription — no separate API billing.

**If you already use one or more of these CLIs day-to-day, pm-agent is a near-free upgrade** that turns them from solo assistants into a coordinated team.

> **What you need:** at least one CLI subscription (Claude Pro/Max recommended). Two or three for true parallelism.

---

## Quick Start

### Prerequisites

- **Python 3.11+** and **git 2.30+**
- At least one of these CLIs installed and logged in:
  - [Claude Code CLI](https://docs.anthropic.com/claude-code) (recommended; Pro or Max subscription)
  - [Codex CLI](https://github.com/openai/codex) (OpenAI Plus or Pro)
  - [Gemini CLI](https://github.com/google/generative-ai-cli) (Google AI Pro)
- **Windows**: WSL2 strongly recommended (native Windows is best-effort)

### Install

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e .

pm-agent --version
pm-agent workers list   # Should list: claude_code, codex, gemini
```

### Make sure your CLIs are logged in

```bash
claude /login        # Uses your Pro/Max subscription
codex login          # Uses your OpenAI subscription
gemini auth login    # Uses your Google account
```

### Your first project

```bash
# 1. Hand it a requirement
pm-agent init my-todo-app \
    --requirement "A Next.js todo app with Google OAuth and PostgreSQL" \
    --budget 30 \
    --max-concurrent 3

# 2. Review and approve the plan PM drafted
cat projects/my-todo-app/PROJECT.md       # The plan
cat projects/my-todo-app/GUARDRAILS.md    # The boundaries
pm-agent reply my-todo-app "approved, but use Drizzle not Prisma"

# 3. Walk away. Watch from your phone if you like.
pm-agent watch my-todo-app
```

---

## What pm-agent Does Well

- 🤖 **Multi-CLI orchestration** — Mix Claude Code, Codex, Gemini, or any CLI you write a 50-line plugin for
- 🚀 **True parallelism** — Up to N workers simultaneously, with file-lock arbitration preventing conflicts
- 🧠 **Persistent project memory** — 5-section `MEMORY.md` auto-distills, keeps context tight across long projects
- 🛡️ **Project Guardrails** — `GUARDRAILS.md` defines what PM may NOT decide alone (frameworks, scope, security)
- 💰 **Triple-layer cost control** — Per-call token caps + per-task budget + project budget; auto-degrades concurrency at 70% spend
- 🔒 **Worker isolation** — Each task in its own `git worktree`; failures auto-cleanup with snapshot archival
- 🔁 **Self-healing** — Stuck-loop detection, auto-retry with worker-type switching, orphan process cleanup
- 📋 **CLI-only by design** — `pm-agent watch <project>` for live status; no web UI, no daemon
- 👁️ **Fully transparent** — Every PM decision logged to `decisions.log`; you can see why it did what

## What pm-agent Doesn't Do (be honest)

- ❌ **Doesn't replace your judgment on big architecture calls** — PM will escalate to you for those
- ❌ **Doesn't scale to massive monorepos** (>100k LOC) — worker context windows limit task size
- ❌ **Doesn't work well with Pro tier alone** — long PM sessions can hit Pro quota; Max recommended
- ❌ **Won't make a bad spec good** — garbage requirements in, garbage project out
- ❌ **Native Windows is best-effort** — WSL2 strongly recommended for now

---

## How It Works (Architecture Overview)

Three roles, one rhythm:

```
┌─────────────┐
│    Boss     │  You. Approve PROJECT.md/GUARDRAILS once,
│   (human)   │  respond to the occasional escalation.
└──────┬──────┘
       │
┌──────▼──────────┐
│   Driver Loop   │  Python process. Owns control flow.
│   (Python)      │  Enforces guardrails, manages workers,
└──────┬──────────┘  tracks cost, recovers from crashes.
       │
┌──────▼──────────┐
│    PM Agent     │  Long Claude Code session.
│   (the brain)   │  Decomposes tasks, picks workers, writes
└──────┬──────────┘  worker prompts. Outputs JSON decisions.
       │
┌──────▼─────────────────────────┐
│    Worker Pool (N=3 default)   │
│    ┌─────┐  ┌─────┐  ┌─────┐  │
│    │ CC  │  │Codex│  │Gem. │  │  ← the tentacles
│    └─────┘  └─────┘  └─────┘  │  Each in its own git worktree.
└────────────────────────────────┘  One-shot subprocess per task.
```

The "octopus" nickname comes from this shape: a central brain (PM) coordinates several independent arms (workers) that each run a task in parallel without stepping on each other.

### Why this shape works

- **Driver as Python loop**, not LLM: control flow is deterministic and inspectable. The PM only outputs decisions, not the loop itself.
- **Workers are one-shot subprocesses**: no shared context, no leaking memory across tasks. Each starts fresh, isolated, and dies after one task.
- **Each worker in its own `git worktree`**: file-system-level isolation. Two workers can edit two different parts of the codebase simultaneously without git conflicts.
- **State entirely on disk**: `MEMORY.md`, `TASKS.json`, `decisions.log`. Crash mid-project? `pm-agent resume` and continue.

---

## Technical Highlights (for the curious)

### Cross-platform process management

Worker subprocesses can spawn `npm`, `pnpm`, dev servers — killing the parent isn't enough. pm-agent uses **kernel-level primitives** for clean tree-kills:

| Platform | Primary | Fallback |
|---|---|---|
| Windows | Job Object + `KILL_ON_JOB_CLOSE` | psutil residue scan |
| POSIX | `setsid()` + `killpg(SIGKILL)` | psutil residue scan |

`psutil` is auxiliary (orphan scan, I/O activity tracking). Kill guarantees come from OS primitives — no race window.

### Concurrent worker dispatch

```python
# Driver main loop, simplified:
while not project_done():
    decision = pm.decide_once()                  # PM outputs JSON
    if decision.action == "dispatch_parallel":
        for task in decision.batch:
            pool.submit(task)                    # Up to N concurrent
        task_id, result = pool.wait_any()        # OS-level event, <10ms
        on_worker_complete(task_id, result)
        # Other workers keep running, PM decides what's next
```

Built on `concurrent.futures` with kernel condition variables. No polling. ~0% CPU when idle.

### Failure recovery

Any non-success path triggers cleanup:

```
Worker fails → archive worktree state (diff, new files, stderr tail)
            → git reset --hard <pre_dispatch_sha>
            → git clean -fd
            → mark dirty, force rebuild before retry
```

PM gets failure context in its next decision; can switch worker type or escalate.

### Driver crash recovery

Workers tag themselves with environment variables (`PM_AGENT_PROJECT_ID`, `PM_AGENT_DRIVER_PID`). On restart, the driver scans for orphans whose owning driver is dead and kills them. State is fully on disk; resume is `pm-agent resume <project>`.

---

## CLI Reference

```bash
# Project lifecycle
pm-agent init <id> --requirement "..." [--budget 50] [--max-concurrent 3]
pm-agent start <id>
pm-agent pause <id>
pm-agent resume <id>
pm-agent stop <id>          # Kills workers + cleanup

# Status
pm-agent status <id>        # Snapshot
pm-agent watch <id>         # Live updating dashboard
pm-agent list               # All projects
pm-agent logs <id> [--task task_007] [--tail 50]
pm-agent decisions <id>     # PM decision history

# Boss interaction
pm-agent reply <id> "your response"
pm-agent escalations <id>   # Pending escalations

# Guardrails
pm-agent guardrails <id>            # View
pm-agent guardrails <id> --edit     # $EDITOR
pm-agent guardrails <id> --validate # Syntax check

# Worker management
pm-agent workers list
pm-agent workers test <name>

# Debugging
pm-agent inspect <id> --task task_007
pm-agent memory <id> [--history]
pm-agent cost <id>
pm-agent pool <id>          # Live worker pool state
```

---

## Custom Workers

Plug in any CLI in ~50 lines:

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

## Roadmap

- **v1.1**: Three-tier memory (hot/warm/cold) for ultra-long projects
- **v1.1**: Memory module tags `[frontend]/[backend]` for selective injection
- **v1.2**: Cross-project knowledge graph
- **v1.3**: Computer Use integration (PM browses docs)
- **v2.0**: TUI dashboard upgrade (still no web)

Web UI / Dashboard: **permanently out of scope**. CLI-first by design choice.

---

## Contributing

Early-stage project. Big changes welcome — open an issue first to coordinate.

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e ".[dev]"
pytest                          # 22 tests
ruff check . && mypy pm_agent
```

---

## License

[MIT](LICENSE) — free to use, modify, distribute.

---

<a name="pm-agent-中文"></a>

# pm-agent

> 🐙 **章鱼助手 · Octopus Assistant** — 设计隐喻的友好昵称（见下文）
>
> [English](#pm-agent) | **中文**

**一个"AI 产品经理"，替你监督你已经在用的编码 CLI（Claude Code / Codex / Gemini）。给它一句话需求，你就可以走开。** 不要 API key，不按 token 计费，复用你已经付过的订阅。

[![状态](https://img.shields.io/badge/status-早期开发-orange)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)]()
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Cost](https://img.shields.io/badge/单项目%20API%20成本-%240-brightgreen)]()
[![平台](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20WSL2-green)]()

> ⚠️ **状态：早期开发中。** 架构基本定型，MVP 实现进行中。**不是**生产就绪。

---

## 它解决什么问题

### 你今天用 AI 写代码大概是这样

你想做一个 Next.js todo app，要 Google OAuth。

你打开 Claude Code，输入 *"帮我搭一个 Next.js + Google OAuth + PostgreSQL 的 todo app"*。它搭了点初始代码，问你 *"App Router 还是 Pages Router？"*。你回。它写了几个路由，又问 *"用 Tailwind 吗？"*。你回。20 分钟后又问 *"NextAuth v4 还是 v5？"*。你回。

你这会儿被**钉在屏幕前**，每几分钟回答一次决策，看着一个 CLI 在做一件事。前端等你想后端，后端等你定 ORM。4 小时刷新过去，你只搞出一个半成品。

这是单 CLI 的"**副驾**"模式。你还是项目经理。

### pm-agent 把这个流程变成这样

你给 pm-agent 同样的一句话需求。

它起草一份 `PROJECT.md`（项目宪法）和 `GUARDRAILS.md`（红线），**只问你一次**："这个计划行不行？"你瞄一眼：23 个任务，技术栈合理，范围清楚。你回 *"approved，但用 Drizzle 不要 Prisma"* —— 然后**离开电脑**。

pm-agent 然后**并发派出三个 worker**：
- Worker 1（Claude Code）做前端
- Worker 2（Codex）做后端 API
- Worker 3（Gemini）做 CI/CD

它跟踪每个 worker 进度，谁完成了就决定下一步派什么，失败了就重试或换 CLI 类型，**只在撞到真实边界时找你**（架构岔路、超出范围、预算告警）。

2 小时后你回来。项目搭完了，测试通过。你总共投入大约 30 分钟——开头确认 + 中途几次 escalation。

你的角色从 **司机** 变成了 **审核 escalation 的老板**。

---

## 为什么不用别的 multi-agent 工具？

这领域已经有不少：MetaGPT、OpenDevin、AutoGen、Aider 多文件模式等。它们对普通开发者有一个共同问题：

**都要 API key。** 每次 PM 决策、每个 worker 调用，都按 token 计费给 OpenAI / Anthropic。一个认真的项目（不是玩具）轻松烧掉 **$50–$300 API 费**。

pm-agent 的核心差异：

| 方案 | 跑一个中等项目的成本 |
|---|---|
| 用 Opus/GPT-5 API 直接驱动多 agent | **$80 – $300** |
| 大多数现有的 multi-agent 开源项目（要 API key） | **$30 – $150** |
| **pm-agent + 你已有的 CLI 订阅** | **$0 边际 API 成本**（订阅你已经付过了） |

诀窍：pm-agent 把你**已经付钱的** Claude Code Pro/Max、OpenAI Plus（Codex）、Google AI Pro（Gemini）订阅当 worker pool 用。PM 自己也跑在一个 Claude Code 长 session 上——没有独立的 API 计费。

**如果你已经在日常用这些 CLI 之一，pm-agent 几乎是个 free upgrade**，把它们从单飞助手变成协作团队。

> **你需要：** 至少一个 CLI 订阅（推荐 Claude Pro/Max）。装两三家才能真正并发。

---

## 快速开始

### 前置条件

- **Python 3.11+** 和 **git 2.30+**
- 至少装好其中一个 CLI 并已登录：
  - [Claude Code CLI](https://docs.anthropic.com/claude-code)（推荐；Pro 或 Max 订阅）
  - [Codex CLI](https://github.com/openai/codex)（OpenAI Plus 或 Pro）
  - [Gemini CLI](https://github.com/google/generative-ai-cli)（Google AI Pro）
- **Windows 用户**：强烈推荐 WSL2（原生 Windows 是 best-effort）

### 安装

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e .

pm-agent --version
pm-agent workers list   # 应该列出: claude_code, codex, gemini
```

### 确认你的 CLI 已登录

```bash
claude /login        # 用你的 Pro/Max 订阅
codex login          # 用你的 OpenAI 订阅
gemini auth login    # 用你的 Google 账号
```

### 你的第一个项目

```bash
# 1. 给它一句话需求
pm-agent init my-todo-app \
    --requirement "用 Next.js + Google OAuth + PostgreSQL 做一个 todo app" \
    --budget 30 \
    --max-concurrent 3

# 2. 审阅 + 批准 PM 起草的计划
cat projects/my-todo-app/PROJECT.md       # 看看规划
cat projects/my-todo-app/GUARDRAILS.md    # 看看边界
pm-agent reply my-todo-app "approved，但用 Drizzle 不要 Prisma"

# 3. 走开。想看进度可以从手机看。
pm-agent watch my-todo-app
```

---

## pm-agent 擅长什么

- 🤖 **多 CLI 编排** — 混搭 Claude Code、Codex、Gemini，或写 50 行插件接入任意 CLI
- 🚀 **真并发** — 最多 N 个 worker 同时跑，文件锁仲裁防冲突
- 🧠 **持久化项目记忆** — 5 段 `MEMORY.md` 自动蒸馏，长项目不爆 context
- 🛡️ **项目护栏** — `GUARDRAILS.md` 定义 PM 不可独自决策的事项（框架、范围、安全）
- 💰 **三层成本控制** — 单调用 token 上限 + 单任务预算 + 项目预算；70% 自动降并发
- 🔒 **Worker 隔离** — 每个任务独立 `git worktree`；失败自动清理 + 快照归档
- 🔁 **自愈** — 卡死检测、自动切换 worker 类型重试、孤儿进程扫描清理
- 📋 **CLI-only 设计** — `pm-agent watch <项目>` 看实时状态；不做 Web UI、不做 daemon
- 👁️ **完全透明** — 每个 PM 决策都记到 `decisions.log`；你能看到它为什么做了那个决定

## pm-agent 不擅长什么（说实话）

- ❌ **不能替你做大架构决策** — PM 会 escalate 给你
- ❌ **不能搞定超大 monorepo**（> 10 万行）— worker context 窗口限制单任务大小
- ❌ **Pro 单订阅可能不够** — PM 长 session 可能撞 Pro 配额；推荐 Max
- ❌ **垃圾需求出垃圾项目** — 烂 spec 不能凭 AI 救
- ❌ **原生 Windows 是 best-effort** — 现阶段强烈推荐 WSL2

---

## 它怎么工作（架构概览）

三个角色，一种节奏：

```
┌─────────────┐
│    Boss     │  你。一次性确认 PROJECT/GUARDRAILS，
│   (人类)    │  偶尔回应 escalation。
└──────┬──────┘
       │
┌──────▼──────────┐
│   Driver Loop   │  Python 进程。掌控循环。
│   (Python)      │  执行护栏、管理 worker、追踪成本、
└──────┬──────────┘  崩溃恢复。
       │
┌──────▼──────────┐
│    PM Agent     │  长 Claude Code session。
│    (大脑)       │  拆分任务、选 worker、写 worker prompt。
└──────┬──────────┘  输出 JSON 决策。
       │
┌──────▼─────────────────────────┐
│   Worker Pool（默认 N=3）      │
│    ┌─────┐  ┌─────┐  ┌─────┐  │
│    │ CC  │  │Codex│  │Gem. │  │  ← 触手
│    └─────┘  └─────┘  └─────┘  │  各自在独立 git worktree。
└────────────────────────────────┘  一次性 subprocess。
```

"章鱼"昵称就是这个形状：中央大脑（PM）协调几条独立触手（worker），各做一份任务，互不踩脚。

### 为什么这个形状能工作

- **Driver 是 Python loop**（不是 LLM）：控制流确定可观测。PM 只输出决策，不掌控循环。
- **Worker 是一次性 subprocess**：没有共享 context，记忆不会跨任务泄漏。每个 worker 起新的、隔离的，跑完就死。
- **每个 worker 在自己的 `git worktree`**：文件系统级隔离。两个 worker 可以同时改不同部分的代码不会 git 冲突。
- **状态全部落盘**：`MEMORY.md`、`TASKS.json`、`decisions.log`。中途崩溃？`pm-agent resume` 继续。

---

## 技术亮点（给好奇的开发者）

### 跨平台进程管理

Worker subprocess 会派生 `npm` / `pnpm` / dev server 等子进程——杀父进程不够。pm-agent 用**内核级原语**做干净的进程树清理：

| 平台 | 主力 | 双保险 |
|---|---|---|
| Windows | Job Object + `KILL_ON_JOB_CLOSE` | psutil 残留扫描 |
| POSIX | `setsid()` + `killpg(SIGKILL)` | psutil 残留扫描 |

`psutil` 只做*辅助*（孤儿扫描、I/O 活跃度跟踪）—— kill 的硬保证靠 OS 原语，无 race window。

### 并发 Worker 调度

```python
# Driver 主循环，简化版：
while not project_done():
    decision = pm.decide_once()                  # PM 输出 JSON
    if decision.action == "dispatch_parallel":
        for task in decision.batch:
            pool.submit(task)                    # 最多 N 并发
        task_id, result = pool.wait_any()        # OS 级事件，延迟 <10ms
        on_worker_complete(task_id, result)
        # 其他 worker 继续跑，PM 决策下一步
```

基于 `concurrent.futures` + 内核条件变量。不轮询，空闲时 CPU 占用 ~0%。

### 失败恢复

任何非 success 路径都触发清理：

```
Worker 失败 → 归档 worktree 状态（diff、新文件、stderr 末尾）
           → git reset --hard <派发前 sha>
           → git clean -fd
           → 标 dirty，重派前强制重建
```

PM 在下一轮决策时拿到失败上下文，可切 worker 类型或 escalate。

### Driver 崩溃恢复

Worker 启动时打 env 标记（`PM_AGENT_PROJECT_ID`、`PM_AGENT_DRIVER_PID`）。重启时 driver 扫所属 driver 已死的孤儿 worker，kill 之。状态全部落盘，恢复就是 `pm-agent resume <项目>`。

---

## CLI 命令

```bash
# 项目生命周期
pm-agent init <id> --requirement "..." [--budget 50] [--max-concurrent 3]
pm-agent start <id>
pm-agent pause <id>
pm-agent resume <id>
pm-agent stop <id>          # kill 所有 worker + 清理

# 状态查询
pm-agent status <id>        # 一次性快照
pm-agent watch <id>         # 实时刷新仪表盘
pm-agent list               # 所有项目
pm-agent logs <id> [--task task_007] [--tail 50]
pm-agent decisions <id>     # PM 决策历史

# Boss 交互
pm-agent reply <id> "回复内容"
pm-agent escalations <id>   # 待回复 escalation

# 护栏管理
pm-agent guardrails <id>            # 查看
pm-agent guardrails <id> --edit     # $EDITOR 编辑
pm-agent guardrails <id> --validate # 语法校验

# Worker 管理
pm-agent workers list
pm-agent workers test <name>

# 调试
pm-agent inspect <id> --task task_007
pm-agent memory <id> [--history]
pm-agent cost <id>
pm-agent pool <id>          # 实时 worker pool 状态
```

---

## 自定义 Worker

约 50 行代码就能接入任何 CLI：

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

## 路线图

- **v1.1**：三层记忆（hot/warm/cold）给超长项目
- **v1.1**：记忆模块标签 `[frontend]/[backend]` 选择性注入
- **v1.2**：跨项目知识图谱
- **v1.3**：Computer Use 集成（PM 浏览文档）
- **v2.0**：TUI 仪表盘升级（仍然不做 Web）

Web UI / Dashboard：**永久排除**。CLI-first 是设计选择。

---

## 贡献

早期项目。欢迎大改动，但请先开 issue 协调。

```bash
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e ".[dev]"
pytest                          # 22 个测试
ruff check . && mypy pm_agent
```

---

## 许可

[MIT](LICENSE) — 自由使用、修改、分发。
