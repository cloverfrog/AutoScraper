# Agent 执行追踪

Agent CLI 默认将每个任务保存到 `.agent-runtime/runs/<run_id>/`。该目录已被 Git
忽略。追踪不调用额外 LLM，不改变任务状态、最终 stdout JSON、规则/数据格式或退出码。

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "前25本书的书名和价格" \
  --spec-output output/books.json --runs-dir .agent-runtime/runs
```

`--runs-dir` 只改变追踪目录。独立 Executor CLI 不默认创建 Run，Python 调用可显式
启用核心接口。每个 `Trace` 表示一个 Run，应为每个任务创建新实例：

```python
from autoscraper.tracing import FileSink, Trace
from autoscraper.workflow import run_task

trace = Trace(FileSink(".agent-runtime/runs"))
outcome = run_task(url, task, settings, trace=trace)
print(trace.run_id, outcome.status)
```

不传 `trace`、也不在显式 Run 上下文中时，Python API 保持不落盘的默认行为。核心
模块只依赖标准库，不需要导入 browser-use；执行器仍可独立使用。

## 文件与事件

```text
<run_id>/
  events.jsonl
  console.log
  summary.json
  history/
    attempt-1.jsonl
    attempt-2.jsonl
```

事件、控制台记录和经过筛选的 History 在运行中逐条写入并 flush，不等任务结束。
History 每行对应框架的一条 History，复用其步骤编号、时间元数据、动作及结果摘要；
不包含完整 browser-use History 的网页/模型正文。使用 JSONL 避免每个步骤重写整个
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

`finished.payload.outcome` 描述调用是否正常返回或抛出异常；工具还会检查
`ActionResult.error` 并标记 `outcome: "error"`。业务任务是否满足要求应读取
`summary.json.outcome.status/task_fulfilled`，不能把“调用返回”当成“任务成功”。
`ask_user` 返回 `success: false` 是请求澄清，并不自动算作工具异常。

步骤开始时固定编号，结束时沿用；模型计划和实际工具调用分开记录，被框架跳过的排队
动作不计入工具次数。取消/异常使框架结束钩子漏触发时，由外层关闭步骤并记录
`incomplete: true`。澄清与回答后恢复仍属于同一 Run / Attempt；修复才增加 Attempt。

## 汇总与指标

`summary.json.outcome` 来自原有 `TaskOutcome.summary()`，经过同一隐私处理，不建立
第二套业务状态判断；`metadata` 保存脱敏后的原始入口、需求摘要和模型。`termination`
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

默认不保存网页正文、DOM、截图、完整模型请求/响应、原始人工问答或任意异常原文。
原始需求和自由文本只保留字符数；URL 只保留协议和域名，去掉用户凭据、端口、路径、
查询和片段；CSS 保留结构，删除引号中的字面值，已知 API Key 也会替换。不要把 CSS
结构当成经过匿名化的数据集；选择器中仍可能存在站点专用类名或 ID。

事件、History、summary 与 console 使用同一处理规则。console 保存事件和框架日志的
级别/来源元数据，原始消息、异常堆栈和格式化参数被省略，因此不是终端逐字副本。
原终端显示不被重定向。追踪 handler 直接监听 `autoscraper`、`browser_use` 和 `bubus`
命名空间，不提前配置根 logger，以保留框架的终端日志初始化；通过任务上下文过滤，
避免把并发 Run 的日志混写。不在当前任务上下文中的后台线程日志不采集。

字符串、集合长度、递归深度和总访问节点数都有上限；单条事件和 History 的 payload
约 12 KB，过长时保留截断标记。默认单 Run 写入预算为 20 MB（包括 console 和 History），
其中预留 64 KB 尝试写最终汇总。目录/文件创建权限分别为 0700/0600。

消费者写入失败或达到限额后明确向 stderr 警告一次，停用该消费者的增量写入；其他
消费者与抓取继续，`trace_errors` 标记不完整。最终仍尝试写 summary；磁盘不可用时
无法保证生成任何文件。强制杀进程不能保证有最终汇总，但此前已 flush 的记录尽可能保留。

## 扩展消费者与验证

`Trace(*sinks)` 可接多个消费者。可选方法为 `start(run_id)`、`event(dict)`、
`console(dict)`、`history(attempt, dict)`、`summary(dict)`；数据内容在交给消费者前
完成处理。通过 `emit(event_type, **payload)` 可在核心层记录事件，用 `labels(...)`
关联上下文，或用 `operation(name, **payload)` 记录有异常兜底的调用边界。
消费者应保持快速、无副作用；暂不提供异步队列、数据库或实时推送。

```bash
uv run --extra agent pytest tests/test_tracing.py -q
uv run --extra agent pytest tests/test_clarification.py -q
```

前者无需真实浏览器或付费 LLM，覆盖写入失败、体积限制、隐私、并发、LLM Usage、
修复、CLI 契约及异常/取消；后者包含真实 Chromium 和本机 HTTP 页面的澄清恢复回归，
使用脚本化 LLM，检查实际步骤、工具和 History 的关联与持久化。
