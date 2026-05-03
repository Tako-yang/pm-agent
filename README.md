# 🐙 Octopus Assistant · 章鱼助手

> Project: **pm-agent** · **English** | [中文](#章鱼助手)

A local-first multi-agent development orchestrator. Give it a one-sentence requirement; it autonomously plans, dispatches, and coordinates multiple coding CLIs (Claude Code / Codex / Gemini) to take your project from zero to delivery — while you sip coffee.

[![Status](https://img.shields.io/badge/status-early%20development-orange)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)]()
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20WSL2-green)]()

> ⚠️ **Status: Early development.** Architecture is largely settled, MVP implementation in progress. Not production-ready. Expect breaking changes.

---

## Why "Octopus"?

An octopus has **eight arms that move independently but coordinate through a central brain**. Each arm can taste, grip, and act on its own, while the brain decides which arms to deploy where.

That's exactly the architecture here:

- **The brain** = a PM Agent (Claude Opus, long-session) — plans, decomposes, decides
- **The arms** = N coding-CLI workers (Claude Code, Codex, Gemini, your custom CLI) — each runs a task in its own isolated `git worktree`, in parallel
- **The nervous system** = the Driver loop — dispatches work, prevents conflicts, recovers from failures

You feed in one sentence. The PM-octopus splits it into tasks, sends each tentacle off with its piece, watches them work simultaneously, pulls in the results, decides what's next. You only step in when it hits a real boundary — a guardrail, a budget cap, an architectural fork.

---

## What is pm-agent?

Most AI coding tools (Claude Code, Codex, Gemini CLI) are **single-shot assistants** — you ask, they answer, repeat. pm-agent flips this:

- **PM Agent** (driven by a long-session CLI) breaks down requirements, plans tasks, and dispatches workers
- **Worker Agents** (Claude Code / Codex / Gemini / your custom CLI) execute coding tasks in parallel, isolated `git worktree`s
- **Driver Loop** (Python) owns the control flow, manages concurrency, enforces guardrails, and persists state
- **Structured Memory** (5-section `MEMORY.md`, ≤3000 chars) keeps the PM oriented across hundreds of decisions without context bloat

You go from *"AI helps me write code"* to *"I manage an AI development team."*

```
You: "Build a Next.js todo app with Google OAuth and PostgreSQL."
  ↓
PM Agent (Claude Opus, long session)  ← the brain
  ├─ Drafts PROJECT.md + GUARDRAILS.md     → asks you once for sign-off
  ├─ Decomposes into 23 tasks
  └─ Sends out tentacles in parallel:
        Tentacle 1 (Claude Code) → frontend
        Tentacle 2 (Codex)       → backend API
        Tentacle 3 (Gemini)      → CI/CD
  ↓
~2 hours later, fully tested project delivered.
You spent ~30 minutes total intervening.
```

---

## Key Features

- 🐙 **Multi-tentacle orchestration** — Mix Claude Code, Codex, Gemini, or any CLI you write a 50-line plugin for
- 🚀 **True parallelism** — Up to N workers run simultaneously with file-lock arbitration preventing conflicts
- 🧠 **Structured long-term memory** — 5-section `MEMORY.md` auto-distills, no context explosion across long projects
- 🛡️ **Project Guardrails** — `GUARDRAILS.md` defines what PM may NOT decide alone (frameworks, scope, security)
- 💰 **Triple-layer cost control** — Per-call token caps + per-task budget + project budget with auto-degradation at 70%
- 🔒 **Worker isolation** — Each task runs in its own `git worktree`, failures auto-cleanup with snapshot archival
- 🪟 **Cross-platform** — Linux, macOS, WSL2 first-class; native Windows supported (with caveats)
- 🔁 **Self-healing** — Stuck-loop detection, auto-retry with worker-type switching, orphan process cleanup
- 📋 **CLI-only by design** — No web UI, no daemon mode, no surprise — `pm-agent watch <project>` is all you need

---

## How It Differs

| | Cursor / Copilot | Claude Code (solo) | **pm-agent** |
|---|---|---|---|
| Granularity | Line/block completion | Single task per session | Whole-project autopilot |
| Your role | Driver | Co-pilot | Reviewer of escalations |
| Parallelism | None | None | N workers concurrently |
| Memory | Session-bound | Session-bound | Persistent, structured, distilled |
| State on crash | Lost | Lost | All on disk, resumable |
| Cost control | Manual | Manual | Triple-layer hard caps |
| Concurrent projects | 1 | 1 | 2-4 typical |

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **git 2.30+** (for `git worktree` support)
- At least one of:
  - [Claude Code CLI](https://docs.anthropic.com/claude-code) (recommended; subscription auth supported)
  - [Codex CLI](https://github.com/openai/codex)
  - [Gemini CLI](https://github.com/google/generative-ai-cli)
- **Recommended**: Claude Pro or Max subscription for the PM session
- **Windows users**: WSL2 strongly recommended (native Windows is best-effort)

### Installation

```bash
# Install from source (no PyPI release yet)
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e .

# Verify
pm-agent --version
pm-agent workers list   # Should show: claude_code, codex, gemini
```

### Authentication

pm-agent expects each CLI to be already authenticated:

```bash
# Claude Code: log in once (uses your Pro/Max subscription)
claude /login

# Codex
codex login

# Gemini
gemini auth login
```

The PM Agent uses Claude Code in long-session mode by default, riding your Max subscription — **no extra API costs** for PM decisions.

### Your first project

```bash
# 1. Create a new project
pm-agent init my-todo-app \
    --requirement "A Next.js todo app with Google OAuth and PostgreSQL" \
    --budget 30 \
    --max-concurrent 3

# 2. PM drafts PROJECT.md and GUARDRAILS.md, then waits for your approval
$ cat projects/my-todo-app/PROJECT.md       # Review the plan
$ cat projects/my-todo-app/GUARDRAILS.md    # Review the boundaries

# 3. Approve (optionally with adjustments)
pm-agent reply my-todo-app "approved, but use Drizzle not Prisma"

# 4. Watch it work
pm-agent watch my-todo-app
```

That's it. PM will dispatch workers, handle failures, distill memory, and eventually deliver. You'll get notified when it needs you.

---

## Core Concepts

### The Three Roles

```
┌─────────────┐
│    Boss     │  You. Approves PROJECT.md/GUARDRAILS once,
│   (human)   │  responds to occasional escalations.
└──────┬──────┘
       │
┌──────▼──────────┐
│   Driver Loop   │  Python process. Owns control flow.
│    (Python)     │  Enforces guardrails, manages workers,
└──────┬──────────┘  tracks cost, recovers from crashes.
       │
┌──────▼──────────┐
│    PM Agent     │  Claude Opus, long CLI session.
│  (Claude Opus)  │  Decomposes tasks, picks workers, writes
│  ← the brain    │  worker prompts. Outputs JSON decisions.
└──────┬──────────┘
       │
┌──────▼─────────────────────────┐
│    Worker Pool (N=3 default)   │
│    ┌─────┐  ┌─────┐  ┌─────┐  │
│    │ CC  │  │Codex│  │Gem. │  │  ← the tentacles
│    └─────┘  └─────┘  └─────┘  │  Each in its own git worktree.
└────────────────────────────────┘  One-shot subprocess.
```

### The 5-Section Memory

Every project maintains a `MEMORY.md` with exactly five sections (≤3000 chars total, auto-distilled when full):

```markdown
# 项目宪法 (Project Constitution) — never changes
- Tech stack: Next.js 14 + PostgreSQL + Drizzle
- Forbidden: Prisma, Jest, CSS Modules

# 当前架构 (Current Architecture) — evolves with implementation
- Auth: NextAuth v5 + Google OAuth, callback /api/auth/callback/google
- API: /api/v1/* with Zod validation

# 已知坑 (Known Pitfalls) — workers MUST read
- Tailwind 4 @apply deprecated, use @utility
- Drizzle migrate has Windows path bugs, use WSL

# 当前未完成任务的上下文 (Active Task Context) — only running tasks
- task_007: implementing OAuth, depends on task_003 (session table) ✓

# 已完成里程碑 (Completed Milestones) — one line each
- ✅ Project scaffolding & CI (tasks 001-002)
- ✅ Database schema (tasks 003-005)
```

When MEMORY exceeds 3000 chars, PM is asked to distill — old version archived to `MEMORY.history/`.

### Project Guardrails

`GUARDRAILS.md` is what stops PM from going off the rails:

```markdown
## Tech Stack Red Lines
forbidden_dependencies: [prisma, jest, redux]
required_stack: {framework: next.js@14, database: postgresql}

## Scope Red Lines
out_of_scope: [payment, i18n, admin_dashboard]

## Security Red Lines
forbidden_patterns:
  - "API_KEY\\s*=\\s*['\"]"   # No hardcoded keys
  - "eslint-disable"            # No silencing the linter

## Decision Red Lines (PM MUST escalate)
must_escalate:
  - introduce_new_framework
  - modify_guardrails
  - add_paid_service
```

PM has high autonomy *within* these boundaries. Driver enforces them as a second layer of defense.

---

## Architecture Highlights

### Cross-platform process management

Worker subprocesses can spawn npm/pnpm/dev servers; killing the parent isn't enough. pm-agent uses **kernel-level primitives**:

| Platform | Primary | Fallback |
|---|---|---|
| Windows | Job Object + `KILL_ON_JOB_CLOSE` | psutil residue scan |
| POSIX | `setsid()` + `killpg(SIGKILL)` | psutil residue scan |

`psutil` is the *auxiliary* layer (orphan scan, I/O activity tracking) — kill guarantees come from OS primitives, with no race window.

### Concurrent worker management

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

Built on `concurrent.futures.as_completed` — kernel condition variables, no polling, ~0% CPU when idle.

### Failure recovery

Any non-success path triggers cleanup:

```
Worker fails → Archive worktree state (diff, new files, stderr tail)
            → git reset --hard <pre_dispatch_sha>
            → git clean -fd
            → Mark dirty, force rebuild before retry
```

PM gets failure context in next decision; can switch worker type or escalate.

### Driver crash recovery

Workers tag themselves with environment variables (`PM_AGENT_PROJECT_ID`, `PM_AGENT_DRIVER_PID`, `PM_AGENT_BORN_AT`). On restart, driver scans for orphans whose owning driver is dead and kills them. State is fully on disk; resume is `pm-agent resume <project>`.

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
pm-agent status <id>        # One-shot snapshot
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
pm-agent workers register <name>    # Wizard for adding custom workers

# Debugging
pm-agent inspect <id> --task task_007
pm-agent memory <id> [--history]
pm-agent cost <id>
pm-agent pool <id>          # Live worker pool state
```

---

## Custom Workers

Plug in any CLI in ~50 lines. Example for Aider:

```python
# ~/.pm-agent/plugins/aider_worker.py
from pm_agent.workers.base import WorkerDispatcher

class AiderWorker(WorkerDispatcher):
    cli_name = "aider"

    def build_command(self, worktree):
        return ["aider", "--yes-always", "--no-show-model-warnings",
                "--message", "-"]

    def get_env(self, use_api_key):
        env = os.environ.copy()
        if not use_api_key:
            env.pop("OPENAI_API_KEY", None)
        return env

    def estimate_cost(self, stdout, stderr):
        return parse_aider_cost(stderr)
```

Register in `~/.pm-agent/workers.yaml`:

```yaml
workers:
  aider:
    module: aider_worker
    class: AiderWorker
```

Verify: `pm-agent workers test aider`

See [`docs/CUSTOM_WORKERS.md`](docs/CUSTOM_WORKERS.md) for the full guide.

---

## Limitations & Roadmap

### Known limitations (MVP)

- ❌ Single PM per project (no parallel PMs on same project)
- ❌ No web UI — CLI only, by design
- ❌ Cross-project memory sharing not yet supported
- ❌ Native Windows is best-effort (WSL2 recommended)
- ❌ Pro subscription typically too small for serious projects (Max recommended)

### Roadmap

- **v1.1**: Three-tier memory (hot/warm/cold) for ultra-long projects
- **v1.1**: Memory module tags `[frontend]/[backend]` for selective injection
- **v1.2**: Cross-project knowledge graph
- **v1.3**: Computer Use integration (PM browses docs)
- **v2.0**: TUI dashboard upgrade (still no web)

Web UI / Dashboard: **permanently out of scope**. pm-agent is CLI-first by design choice.

---

## Documentation

- [Custom Workers Guide](docs/CUSTOM_WORKERS.md) — write a 50-line plugin to add any CLI
- Product Requirements (PRD) — *planned*
- Technical Design (TDD) — *planned*
- Concurrency Design (`WorkerPool` + `as_completed` deep dive) — *planned*
- Process Management (Job Object / setsid layered design) — *planned*
- Memory System (5-section structure, distillation) — *planned*
- Guardrails (defining PM autonomy boundaries) — *planned*

---

## Contributing

Early-stage project. Big changes welcome but coordinate via issues first.

```bash
# Dev setup
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e ".[dev]"
pytest                          # Run 22 tests
ruff check . && mypy pm_agent   # Lint
```

---

## License

[MIT](LICENSE) — free to use, modify, distribute.

---

<a name="章鱼助手"></a>

# 🐙 章鱼助手 · Octopus Assistant

> 项目代号：**pm-agent** · [English](#-octopus-assistant--章鱼助手) | **中文**

本地优先的多 Agent 自动开发编排系统。给它一句话需求，它自主拆分、派发、协调多个编码 CLI（Claude Code / Codex / Gemini）将你的项目从 0 到 1 交付——你只需要喝杯咖啡。

[![状态](https://img.shields.io/badge/status-早期开发-orange)]()
[![Python](https://img.shields.io/badge/python-3.11+-blue)]()
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![平台](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20WSL2-green)]()

> ⚠️ **状态：早期开发中**。架构基本定型，MVP 实现进行中。**不是**生产就绪。预期会有 breaking changes。

---

## 为什么叫"章鱼"？

章鱼有 **8 条独立运动但通过中央大脑协调** 的触手。每条触手能独立感知、抓取、行动；大脑负责决定哪条触手去哪里干什么。

这正是本项目的架构：

- **大脑** = PM Agent（Claude Opus 长 session）—— 规划、拆分、决策
- **触手** = N 个编码 CLI Worker（Claude Code、Codex、Gemini，或你的自定义 CLI）—— 各自在隔离的 `git worktree` 内并发跑任务
- **神经系统** = Driver 主循环 —— 派发工作、防止冲突、错误恢复

你给一句话。章鱼 PM 拆成多个任务，每条触手带一份去干，并发运行，把结果收回来，决定下一步。你只在它撞到真实边界时介入——护栏、预算、架构岔路。

---

## pm-agent 是什么？

主流 AI 编码工具（Claude Code、Codex、Gemini CLI）都是**单次问答助手**——你问它答，反复循环。pm-agent 反过来：

- **PM Agent**（基于 CLI 长 session 驱动）拆分需求、规划任务、派发 Worker
- **Worker Agents**（Claude Code / Codex / Gemini / 你的自定义 CLI）在隔离 `git worktree` 内并发执行编码任务
- **Driver Loop**（Python）掌控循环、管理并发、执行护栏、持久化状态
- **结构化记忆**（5 段 `MEMORY.md`，≤ 3000 字）在数百轮决策中保持 PM 状态清晰，不爆 context

你的角色从 *"AI 帮我写代码"* 升级为 *"我管理一个 AI 开发团队"*。

```
你："做一个用 Next.js + Google OAuth + PostgreSQL 的 todo 应用"
  ↓
PM Agent（Claude Opus 长 session）  ← 大脑
  ├─ 起草 PROJECT.md + GUARDRAILS.md   → 找你一次性确认
  ├─ 拆分为 23 个任务
  └─ 触手并发出动：
        触手 1（Claude Code）→ 前端
        触手 2（Codex）      → 后端 API
        触手 3（Gemini）     → CI/CD
  ↓
~2 小时后，完整测试通过的项目交付。
你总共投入约 30 分钟。
```

---

## 核心特性

- 🐙 **多触手编排** — 混搭 Claude Code、Codex、Gemini，或写 50 行插件接入任意 CLI
- 🚀 **真并发** — 最多 N 个 worker 同时跑，文件锁仲裁防冲突
- 🧠 **结构化长期记忆** — 5 段 `MEMORY.md` 自动蒸馏，长项目不爆 context
- 🛡️ **项目护栏** — `GUARDRAILS.md` 定义 PM 不可独自决策的事项（框架、范围、安全）
- 💰 **三层成本控制** — 单调用 token 上限 + 单任务预算 + 项目预算，70% 自动降并发
- 🔒 **Worker 隔离** — 每个任务独立 `git worktree`，失败自动清理 + 快照归档
- 🪟 **跨平台** — Linux / macOS / WSL2 一等支持；原生 Windows 二等支持
- 🔁 **自愈** — 卡死检测、自动切换 worker 类型重试、孤儿进程扫描清理
- 📋 **CLI-only 设计** — 不做 Web UI、不做 daemon、不搞花样，`pm-agent watch <项目>` 就够了

---

## 为什么不是别的工具？

| | Cursor / Copilot | Claude Code（单用）| **pm-agent** |
|---|---|---|---|
| 粒度 | 行/块补全 | 单 session 单任务 | 整项目自动 |
| 你的角色 | 驾驶员 | 副驾 | 升级时的审核者 |
| 并发 | 无 | 无 | N 个 worker 同时跑 |
| 记忆 | 仅 session | 仅 session | 持久、结构化、自动蒸馏 |
| 崩溃后状态 | 丢失 | 丢失 | 全部落盘，可恢复 |
| 成本控制 | 手动 | 手动 | 三层硬护栏 |
| 同时跑项目数 | 1 | 1 | 2-4 个常态 |

---

## 快速开始

### 前置条件

- **Python 3.11+**
- **git 2.30+**（`git worktree` 支持）
- 至少装一个：
  - [Claude Code CLI](https://docs.anthropic.com/claude-code)（推荐；支持订阅认证）
  - [Codex CLI](https://github.com/openai/codex)
  - [Gemini CLI](https://github.com/google/generative-ai-cli)
- **推荐**：Claude Pro 或 Max 订阅（给 PM session 用）
- **Windows 用户**：强烈推荐 WSL2（原生 Windows 是 best-effort）

### 安装

```bash
# 暂无 PyPI 发布，从源码装
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e .

# 验证
pm-agent --version
pm-agent workers list   # 应该列出: claude_code, codex, gemini
```

### 认证

pm-agent 假设各 CLI 已经登录：

```bash
# Claude Code：登一次（吃你的 Pro/Max 订阅）
claude /login

# Codex
codex login

# Gemini
gemini auth login
```

PM Agent 默认用 Claude Code 长 session 模式，走你的 Max 订阅——**PM 决策不额外产生 API 费**。

### 第一个项目

```bash
# 1. 创建新项目
pm-agent init my-todo-app \
    --requirement "用 Next.js + Google OAuth + PostgreSQL 做 todo 应用" \
    --budget 30 \
    --max-concurrent 3

# 2. PM 起草 PROJECT.md 和 GUARDRAILS.md，等你确认
$ cat projects/my-todo-app/PROJECT.md       # 看看规划
$ cat projects/my-todo-app/GUARDRAILS.md    # 看看边界

# 3. 批准（可附加调整）
pm-agent reply my-todo-app "approved，但用 Drizzle 不要 Prisma"

# 4. 实时观察
pm-agent watch my-todo-app
```

到此为止。PM 会派 worker、处理失败、蒸馏记忆，最终交付。它需要你时会通知你。

---

## 核心概念

### 三个角色

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
│    PM Agent     │  Claude Opus，长 CLI session。
│  (Claude Opus)  │  拆分任务、选 worker、写 worker prompt。
│   ← 大脑        │  输出 JSON 决策。
└──────┬──────────┘
       │
┌──────▼─────────────────────────┐
│   Worker Pool（默认 N=3）      │
│    ┌─────┐  ┌─────┐  ┌─────┐  │
│    │ CC  │  │Codex│  │Gem. │  │  ← 触手
│    └─────┘  └─────┘  └─────┘  │  各自在独立 git worktree。
└────────────────────────────────┘  一次性 subprocess。
```

### 5 段记忆

每个项目维护一份 `MEMORY.md`，**严格 5 段**（总字数 ≤ 3000，超限自动蒸馏）：

```markdown
# 项目宪法 — 永远不变
- 技术栈：Next.js 14 + PostgreSQL + Drizzle
- 不用：Prisma、Jest、CSS Modules

# 当前架构 — 随实现演进
- 认证：NextAuth v5 + Google OAuth，回调 /api/auth/callback/google
- API：/api/v1/* 风格，Zod 验证

# 已知坑 — worker 必读
- Tailwind 4 @apply 废弃，改用 @utility
- Drizzle migrate 在 Windows 路径有 bug，用 WSL

# 当前未完成任务的上下文 — 只放正在做的
- task_007：实现 OAuth，依赖 task_003 的 session 表 ✓

# 已完成里程碑 — 一句话
- ✅ 项目脚手架与 CI（task_001-002）
- ✅ 数据库 schema（task_003-005）
```

MEMORY 超 3000 字时，PM 被要求蒸馏——旧版归档到 `MEMORY.history/`。

### 项目护栏

`GUARDRAILS.md` 是阻止 PM 跑偏的关键：

```markdown
## 技术栈红线
forbidden_dependencies: [prisma, jest, redux]
required_stack: {framework: next.js@14, database: postgresql}

## 范围红线
out_of_scope: [支付, 国际化, 后台管理]

## 安全红线
forbidden_patterns:
  - "API_KEY\\s*=\\s*['\"]"   # 禁硬编码 key
  - "eslint-disable"           # 禁绕过 lint

## 决策红线（PM 必须升级）
must_escalate:
  - introduce_new_framework  # 引入新框架
  - modify_guardrails        # 修改本文件
  - add_paid_service         # 引入付费服务
```

PM 在边界**内**有高度自主权。Driver 作为第二层兜底执行。

---

## 架构亮点

### 跨平台进程管理

Worker subprocess 会派生 npm/pnpm/dev server 等，杀父进程不够。pm-agent 用**内核级原语**：

| 平台 | 主力 | 双保险 |
|---|---|---|
| Windows | Job Object + `KILL_ON_JOB_CLOSE` | psutil 残留扫描 |
| POSIX | `setsid()` + `killpg(SIGKILL)` | psutil 残留扫描 |

`psutil` 只做*辅助*（孤儿扫描、I/O 活跃度跟踪）——kill 的硬保证靠 OS 原语，无 race window。

### 并发 Worker 管理

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

基于 `concurrent.futures.as_completed`——内核条件变量，不轮询，空闲时 CPU 占用 ~0%。

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

Worker 启动时打 env 标记（`PM_AGENT_PROJECT_ID`、`PM_AGENT_DRIVER_PID`、`PM_AGENT_BORN_AT`）。重启时 driver 扫所属 driver 已死的孤儿 worker，kill 之。状态全部落盘，恢复就是 `pm-agent resume <项目>`。

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
pm-agent workers register <name>    # 引导添加自定义 worker

# 调试
pm-agent inspect <id> --task task_007
pm-agent memory <id> [--history]
pm-agent cost <id>
pm-agent pool <id>          # 实时 worker pool 状态
```

---

## 自定义 Worker

约 50 行代码就能接入任何 CLI。Aider 例子：

```python
# ~/.pm-agent/plugins/aider_worker.py
from pm_agent.workers.base import WorkerDispatcher

class AiderWorker(WorkerDispatcher):
    cli_name = "aider"

    def build_command(self, worktree):
        return ["aider", "--yes-always", "--no-show-model-warnings",
                "--message", "-"]

    def get_env(self, use_api_key):
        env = os.environ.copy()
        if not use_api_key:
            env.pop("OPENAI_API_KEY", None)
        return env

    def estimate_cost(self, stdout, stderr):
        return parse_aider_cost(stderr)
```

注册到 `~/.pm-agent/workers.yaml`：

```yaml
workers:
  aider:
    module: aider_worker
    class: AiderWorker
```

验证：`pm-agent workers test aider`

完整指南见 [`docs/CUSTOM_WORKERS.md`](docs/CUSTOM_WORKERS.md)。

---

## 已知限制 & 路线图

### MVP 已知限制

- ❌ 同一项目单 PM（不支持多 PM 并行）
- ❌ 无 Web UI——只 CLI，设计如此
- ❌ 跨项目记忆共享暂不支持
- ❌ 原生 Windows 是 best-effort（推荐 WSL2）
- ❌ Pro 订阅对认真项目通常不够（推荐 Max）

### 路线图

- **v1.1**：三层记忆（hot/warm/cold）给超长项目
- **v1.1**：记忆模块标签 `[frontend]/[backend]` 选择性注入
- **v1.2**：跨项目知识图谱
- **v1.3**：Computer Use 集成（PM 浏览文档）
- **v2.0**：TUI 仪表盘升级（仍然不做 Web）

Web UI / Dashboard：**永久排除**。pm-agent 是 CLI-first 的设计选择。

---

## 文档

- [自定义 Worker 开发指南](docs/CUSTOM_WORKERS.md) — 50 行写一个新 CLI 插件
- 产品需求文档（PRD） — *待整理*
- 技术设计文档（TDD） — *待整理*
- 并发设计（`WorkerPool` + `as_completed` 详解） — *待整理*
- 进程管理（Job Object / setsid 分层设计） — *待整理*
- 记忆系统（5 段结构、蒸馏机制） — *待整理*
- 项目护栏（定义 PM 自主权边界） — *待整理*

---

## 贡献

早期项目。欢迎大改动，但请先开 issue 协调。

```bash
# 开发环境
git clone https://github.com/Tako-yang/pm-agent.git
cd pm-agent
pip install -e ".[dev]"
pytest                          # 跑测试（22 个）
ruff check . && mypy pm_agent   # lint
```

---

## 许可

[MIT](LICENSE) — 自由使用、修改、分发。
