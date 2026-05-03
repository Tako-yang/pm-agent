你正在为一个新软件项目做启动规划。Boss 给你的需求如下：

# Boss 的需求

{{user_requirement}}

# 你的任务

基于此需求，自主生成以下三个文件，全部放在你的回复中。

## 1. PROJECT.md（项目宪法 + 范围说明）

包含：
- 项目目标（一段话）
- 核心功能列表（≤10 项）
- 明确的"非目标"（不做什么）
- 推荐技术栈（含理由）
- 部署目标
- 测试策略

## 2. GUARDRAILS.md（项目护栏）

基于你对需求的理解，主动定义你认为合理的红线。Boss 会审阅修改。
红线建议偏严格——Boss 觉得太严会自己放宽。

**必须严格使用以下双层格式（人类描述 + 嵌入式 YAML）**，driver 解析器只识别 yaml 块。

必含 4 个 `##` 段，每段下都要有一个 yaml 代码块，块第一行必须是 `# rules: <category>` 注释，类别名固定为：

| 段名 | yaml 类别 | 必含字段 |
|---|---|---|
| 技术栈红线 | `tech_stack` | `forbidden_dependencies`, `required_stack` |
| 范围红线 | `scope` | `out_of_scope` |
| 安全红线 | `security` | `forbidden_patterns` |
| 决策红线（必须升级 Boss）| `must_escalate` | `must_escalate` |

格式示例（必须照搬结构，内容根据需求替换）：

````markdown
## 技术栈红线

<这里写 1-3 句人类可读的"为什么"——给 Boss 和未来的 PM 看>

```yaml
# rules: tech_stack
forbidden_dependencies:
  - <禁用依赖名>
required_stack:
  framework: <必用框架>
  database: <必用数据库>
```

## 范围红线

<人类描述>

```yaml
# rules: scope
out_of_scope:
  - <超范围功能>
```

## 安全红线

<人类描述>

```yaml
# rules: security
forbidden_patterns:
  - "<正则 pattern 1>"
  - "<正则 pattern 2>"
```

## 决策红线（必须升级 Boss）

<人类描述>

```yaml
# rules: must_escalate
must_escalate:
  - introduce_new_framework
  - modify_guardrails
  - add_paid_service
  - deployment_decisions
```
````

**注意**：
- yaml 块的语言标记必须是 ` ```yaml `（小写），不能是 `yml` 或省略
- `# rules: <category>` 注释行不可省略，解析器靠它认类别
- yaml 缩进用 2 空格，禁用 tab
- 4 个类别一个都不能少，driver 启动时会校验

## 3. TASKS.json 初稿

拆分为 8-25 个任务，每个任务必含：
- `id`（task_001_xxx 格式，下划线分隔的 snake_case）
- `title`
- `description`
- `depends_on`（数组，可空）
- `acceptance_criteria`（具体可验证）
- `files_owned`（glob 列表，为后续并发准备）
- 第一批"无依赖任务"标记 `status="pending"`，其余 `status="blocked"`

# 输出格式

严格按以下顺序输出三个代码块，不要其他文本。

**重要**：因为 GUARDRAILS.md 内部包含 ` ```yaml ` 嵌套代码栅，所以 `guardrails_md`
块必须用 **4 个反引号** 包裹，避免与内层冲突。其余两个用 3 反引号即可。

```project_md
<PROJECT.md 内容>
```

````guardrails_md
<GUARDRAILS.md 内容，里面会有 ```yaml ... ``` 块>
````

```tasks_json
{
  "project_id": "<project_id>",
  "version": 1,
  "tasks": [...]
}
```

driver 会解析三个块分别写入文件，然后向 Boss 发起 escalation 请求确认。
