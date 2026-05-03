# Conductor

> 多 Agent 自动开发编排系统 (Multi-Agent automated development orchestration system)
>
> 详见 [PRD](../01_PRD_产品需求文档.md) 和 [TDD](../02_TDD_技术设计文档.md)。

## 快速开始

### 1. 安装

```bash
cd conductor
pip install -e .
```

Windows 用户：依赖中已声明 `pywin32`。
Linux/macOS 用户：`pywin32` 会被 setuptools 自动跳过。

### 2. 设置环境变量

```bash
# PM Agent 用 Claude Opus（必需）
export ANTHROPIC_API_KEY=sk-ant-...

# Worker 默认走订阅认证（OAuth login），可选 API key 备份：
export WORKER_ANTHROPIC_API_KEY=...   # claude_code worker 走 API 时用
export WORKER_OPENAI_API_KEY=...      # codex worker 走 API 时用
export WORKER_GEMINI_API_KEY=...      # gemini worker 走 API 时用
```

### 3. 跑一个 demo

```bash
# 创建项目
conductor init demo --requirement "做一个支持注册登录的 todo 应用，用 Next.js + PostgreSQL"

# Boss 审阅生成的 PROJECT.md / GUARDRAILS.md，编辑后确认
conductor reply demo "approved"

# 启动 driver loop（PM 自主推进）
conductor start demo

# 实时观察
conductor watch demo
```

## 完整 CLI

```bash
# 项目生命周期
conductor init <project_id> --requirement "..." [--budget 50] [--max-concurrent 3]
conductor start <project_id>
conductor pause <project_id>
conductor resume <project_id>
conductor stop <project_id>

# 状态
conductor status <project_id>
conductor watch <project_id>
conductor list
conductor logs <project_id> [--task task_007] [--tail 50]
conductor decisions <project_id> [--tail 20]
conductor inspect <project_id> --task task_007

# Boss 交互
conductor reply <project_id> "..."
conductor escalations <project_id>

# 护栏
conductor guardrails show <project_id>
conductor guardrails edit <project_id>
conductor guardrails validate <project_id>

# Worker 管理
conductor workers list
conductor workers test <name>

# 调试 / 危险
conductor memory <project_id> [--history]
conductor cost <project_id>
conductor pool <project_id>
conductor reset <project_id> --yes
conductor kill <project_id>
```

## 架构概览

```
Boss (人类)
  │  conductor init / reply
  ▼
CLI (typer)
  │
  ▼
Driver Loop (driver.py)             ← Python 控制循环（不是 LLM）
  │  ├── PM Agent (pm.py)            ← 单轮决策 (Claude Opus)
  │  ├── GuardrailsChecker (guardrails.py)  ← 决策兜底校验
  │  ├── CostTracker (cost.py)       ← 三道护栏 + 70%/80%/100% 阈值
  │  ├── EscalationStore (escalation.py)    ← Boss 升级
  │  ├── MemoryCorrectionStore (corrections.py) ← 双重确认
  │  └── WorkerPool (concurrency.py)
  │       ├── FileLockArbiter         ← files_owned 仲裁
  │       ├── WorkerProgressMonitor   ← 卡死/偏离 kill
  │       └── ThreadPoolExecutor → subprocess workers
  │            ├── ClaudeCodeWorker
  │            ├── CodexWorker
  │            ├── GeminiWorker
  │            └── 用户自定义 (~/.conductor/workers.yaml)
  │
  ▼
project files (PROJECT.md / GUARDRAILS.md / MEMORY.md / TASKS.json / .pm/* / worktrees/*)
```

## 项目目录布局

```
conductor/
├── conductor/
│   ├── cli.py              # CLI 入口
│   ├── driver.py           # 外置循环 + 决策执行
│   ├── pm.py               # PM Agent
│   ├── guardrails.py       # 护栏校验
│   ├── memory.py           # MEMORY.md 5 段管理
│   ├── tasks.py            # TASKS.json 状态机
│   ├── feedback.py         # FEEDBACK 块解析
│   ├── concurrency.py      # WorkerPool / FileLockArbiter / ProgressMonitor
│   ├── cost.py             # 成本追踪
│   ├── escalation.py       # Boss 升级机制
│   ├── corrections.py      # memory_corrections 双重确认
│   ├── process_group.py    # 跨平台进程组（POSIX setsid / Windows Job Object）
│   ├── worktree.py         # git worktree 管理
│   ├── project_init.py     # init 命令实现
│   ├── project_store.py    # 项目文件系统抽象
│   ├── status_view.py      # rich CLI 可视化
│   ├── utils.py            # 工具函数
│   ├── workers/
│   │   ├── base.py         # WorkerDispatcher 抽象基类
│   │   ├── registry.py     # 注册表
│   │   ├── claude_code.py  # 内置
│   │   ├── codex.py        # 内置
│   │   └── gemini.py       # 内置
│   ├── prompts/
│   │   ├── pm_system.md
│   │   ├── worker_template.md
│   │   ├── distill.md
│   │   └── init_pm.md
│   └── schemas/
│       ├── tasks.schema.json
│       ├── feedback.schema.json
│       └── decision.schema.json
├── docs/
│   └── CUSTOM_WORKERS.md   # 自定义 Worker 开发指南
├── tests/
│   ├── test_integration.py # 集成测试（mock PM）
│   └── test_scenarios.py   # PRD 5 场景白盒测试
├── pyproject.toml
└── README.md
```

## 核心设计原则

1. **循环控制权外置** —— Driver 是 Python 循环，PM 只做单轮决策。
2. **状态文件化** —— 所有状态写磁盘后才视为已完成，进程崩溃可恢复。
3. **Worker 失忆 + 外部记忆** —— Worker 一次性进程，知识靠 MEMORY.md 注入。
4. **结构化通信** —— Worker 反馈是 `<FEEDBACK>` JSON 块，不是自由文本。
5. **隔离优先于性能** —— Worker 永远独立 git worktree，单任务超时即杀（进程组级）。
6. **PM 高自主 + 护栏兜底** —— PM 自由拆分/分工/并发，但 GUARDRAILS.md 是硬约束。
7. **并发受锁约束** —— files_owned 是并发的"令牌"，没拿到锁不允许跑。
8. **Worker 类型开放扩展** —— 内置 3 种是默认实现，用户可自定义注册新类型。

## 测试

```bash
# 集成测试（不需要真实 ANTHROPIC_API_KEY）
python tests/test_integration.py

# PRD 5 场景白盒测试
python tests/test_scenarios.py
```

## 自定义 Worker

详见 [docs/CUSTOM_WORKERS.md](docs/CUSTOM_WORKERS.md)。约 50 行代码即可实现一个新 Worker 类型。
