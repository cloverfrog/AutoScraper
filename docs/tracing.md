# Agent 执行追踪

Agent CLI 默认将每个任务保存到 `.agent-runtime/runs/<开始时间>_<run_id>/`。该目录已被 Git
忽略。追踪不调用额外 LLM，不改变任务状态、最终 stdout JSON、规则/数据格式或退出码。

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "前25本书的书名和价格" \
  --spec-output output/books.json --runs-dir .agent-runtime/runs
```

`--runs-dir` 只改变追踪目录。独立 Executor CLI 不默认创建 Run，Python 调用可显式
启用核心接口。每个 `Trace` 表示一个 Run，应为每个任务创建新实例：

```python
from autoscraper.observability.tracing import FileSink, Trace
from autoscraper.workflow.runner import run_task

trace = Trace(FileSink(".agent-runtime/runs"))
outcome = run_task(url, task, settings, trace=trace)
print(trace.run_id, outcome.status)
```

不传 `trace`、也不在显式 Run 上下文中时，Python API 保持不落盘的默认行为。核心
模块只依赖标准库，不需要导入 browser-use；执行器仍可独立使用。

## 文件与事件

```text
<开始时间>_<run_id>/
  events.jsonl
  summary.json
  history/
    attempt-1.jsonl
    attempt-2.jsonl
```

目录前缀使用运行机器的本地时间，包含微秒和时区，例如
`20261027-163012-123456+0800_<run_id>`；在同一时区按名称排序即可查看先后顺序。
随机 Run ID 继续用于唯一关联和避免同一时刻重名，事件中的 `run_id` 保持不变。
旧目录不会自动重命名。

事件和经过筛选的 History 在运行中逐条写入并 flush，不等任务结束。
History 每行对应框架的一条 History，复用其步骤编号、时间元数据、动作及结果摘要；
保留实际可用的模型决策摘要和工具反馈，不保存完整 DOM 或完整模型请求/响应。使用 JSONL 避免每个步骤重写整个
数组或在内存缓存全部轨迹。最终 `summary.json` 通过临时文件替换写入。

公共事件字段：`schema_version`（当前为 1）、`run_id`、递增 `seq`、UTC `timestamp`、
`event_type`、`payload`。适用时有 `attempt`、`step`、`call_id`、`parent_call_id` 和
LLM `source`。同一调用的 started / finished 事件使用相同 `call_id`，耗时用单调时钟。

主要事件包括：

- `run.started/finished/failed`、`workflow.started/finished/result`。
- `requirements.started/finished`、`attempt.started/finished`、`repair.started/finished`。
- `step.started/planned/finished`、`tool.started/finished`、`llm.started/finished`。
- `spec.candidate`、`validation.started/finished/feedback`、`validation_extraction.started/finished`。
- `clarification.started/answer/finished`、`execution.started/finished`。
- `executor.started/finished/result`、`artifacts.started/finished`、`cli.result/failed`。
- `framework.log`：框架 WARNING/ERROR 的消息及可用异常栈。

`finished.payload.outcome` 描述调用是否正常返回或抛出异常；工具还会检查
`ActionResult.error` 并标记 `outcome: "error"`。业务任务是否满足要求应读取
`summary.json.outcome.status/task_fulfilled`，不能把“调用返回”当成“任务成功”。
`ask_user` 返回 `success: false` 是请求澄清，并不自动算作工具异常。

步骤开始时固定编号，结束时沿用；模型计划和实际工具调用分开记录，被框架跳过的排队
动作不计入工具次数。取消/异常使框架结束钩子漏触发时，由外层关闭步骤并记录
`incomplete: true`。澄清与回答后恢复仍属于同一 Run / Attempt；修复才增加 Attempt。

## 决策与工具证据

`step.planned.payload.assessment` 及每条 History 的 `assessment` 保存 browser-use
实际提供的 `memory`、`next_goal`、`evaluation_previous_goal`。缺失字段不补造，
不额外调用模型生成解释；这些是模型的判断摘要，不能作为独立验证过的事实。

`tool.finished.payload.feedback` 与 History 的工具结果保存可读反馈。选择器测试
保留数量、样本的匹配基数/状态、样本值及错误消息；unsupported 保留原因和受影响字段；
澄清保留问题、说明和回答。`inspect_page` 和等待工具中的 DOM 只提取有界证据：
页面 URL/标题、报告的总节点数、实际返回节点数、前 8 个节点及最多 12 个链接/按钮等
控件样本。样本保留标签、属性和短文本；样本中未出现某元素，不等于整个页面没有。
原始 inspection 根选择器保留在工具输入中，可用于判断检查范围。

例如 load-more 场景，应能依次看到：按钮样本没有 href；选择器测试只匹配 1 条；
模型摘要判断请求的 2 条无法通过当前分页协议取得；report_unsupported 和最终汇总
保留这个原因。模型陈述和工具观测分开记录，以便检查判断依据。

普通框架 INFO 不重复落盘。WARNING/ERROR 作为 `framework.log` 进入 events.jsonl，
保存消息和可用的异常栈；操作抛出的异常也在对应 finished/failed 事件中保留消息、
类型和异常链/调用栈，不采集局部变量。没有 exc_info 的日志不能凭空恢复堆栈。
终端原有日志保持显示；不再创建重复的 console.log，旧 Run 文件不自动删除。

## 汇总与指标

`summary.json.outcome` 来自原有 `TaskOutcome.summary()`，经过同一隐私处理，不建立
第二套业务状态判断；`metadata` 保存原始入口 URL、任务需求和模型（受长度限制及明确凭据替换规则约束）。`termination`
表示追踪上下文正常返回、异常或中断；CLI 的 `exit_code` 单独保存。规则/数据落盘失败
时保留工作流原结果，通过 `artifacts.finished` / `cli.failed` 和退出码表达产物失败。

`metrics` 包括任务墙钟耗时、各阶段累计耗时、步骤/工具/Attempt/Repair/验证次数、按
来源细分的 LLM 调用次数与 Token。阶段可能嵌套，累计耗时不可直接相加作为总时长。
人工等待计入墙钟耗时及 clarification 阶段，不改变已有活动时间预算。

LLM 来源为 `requirements`、`main`、`wait`。兼容原 `llm_calls` 仍包含需求解析和主
Agent；`wait_llm_calls` 保持独立。追踪中的调用数指适配器调用，包括失败与取消；未
进入适配器的预算拒绝不计。API 已返回 Usage 而结构化解析失败时仍保留 Token。
API 未返回 Usage 时 `tokens: null`；部分可得时只汇总已报告值，并通过 `usage_calls`
和 `usage_missing_calls` 表明覆盖范围。不估算费用。

## 隐私、限额与故障

默认尽量保留诊断内容。普通文本、原始需求、人工问答、失败原因、协议枚举、字段名和
选择器字面值直接保留，不再统一转换为字符数。URL 保留端口、路径、普通查询参数和
片段，不重新编码查询参数；完整 DOM、截图和完整模型请求/响应不作默认采集。

只对明确凭据进行针对性处理：替换已配置的 API Key；精确的认证字段（例如
Authorization、Cookie、api_key、password、access_token）隐藏值，普通 headers
和包含相似字样的诊断字段保持原样；URL 删除用户名/密码，只替换明确认证查询参数的值；
文本中明确的 Authorization: Bearer/Basic 和 Cookie 头行隐藏凭据，保留上下文。
这些规则不是通用敏感内容检测器，不承诺识别任意未知、编码或变形的凭据。

事件、History 和 summary 使用同一处理规则。追踪 handler 监听 `autoscraper`、
`browser_use` 和 `bubus` 命名空间，不提前配置根 logger，以保留框架的终端日志初始化；
通过任务上下文过滤，避免把并发 Run 的日志混写。不在任务上下文的后台线程日志不采集。

字符串、集合长度、递归深度和总访问节点数都有上限；单条事件和 History 的 payload
约 12 KB，过长时压缩文本/样本并保留截断标记，极端情况下保留可读片段。单个文本
最多保留 4096 个字符。默认单 Run 写入预算为 20 MB（包括事件和 History），
其中预留 64 KB 尝试写最终汇总。目录/文件创建权限分别为 0700/0600。

消费者写入失败或达到限额后明确向 stderr 警告一次，停用该消费者的增量写入；其他
消费者与抓取继续，`trace_errors` 标记不完整。最终仍尝试写 summary；磁盘不可用时
无法保证生成任何文件。强制杀进程不能保证有最终汇总，但此前已 flush 的记录尽可能保留。

## 扩展消费者与验证

`Trace(*sinks)` 可接多个消费者。可选方法为 `start(run_id)`、`event(dict)`、
`history(attempt, dict)`、`summary(dict)`；数据内容在交给消费者前
完成处理。通过 `emit(event_type, **payload)` 可在核心层记录事件，用 `labels(...)`
关联上下文，或用 `operation(name, **payload)` 记录有异常兜底的调用边界。
消费者应保持快速、无副作用；暂不提供异步队列、数据库或实时推送。

```bash
uv run --extra agent pytest tests/observability/test_tracing.py -q
uv run --extra agent pytest tests/agent/test_clarification.py -q
```

前者无需真实浏览器或付费 LLM，覆盖写入失败、体积限制、隐私、并发、LLM Usage、
修复、CLI 契约及异常/取消；后者包含真实 Chromium 和本机 HTTP 页面的澄清恢复回归，
使用脚本化 LLM，检查实际步骤、工具和 History 的关联与持久化，同时验证 load-more
unsupported 的工具证据、模型摘要和最终原因能从日志独立读出。
