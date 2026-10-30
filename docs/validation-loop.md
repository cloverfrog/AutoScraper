# Validation + Feedback Repair Loop

解决 [issue #1](https://github.com/cloverfrog/AutoScraper/issues/1)：规则可以通过 Pydantic、执行器也可以正常结束，但仍然遗漏用户要求的字段。现在候选规则不能作为用户需求的真值来源，执行成功和任务完成会分别报告。

## 使用

原 Agent 命令自动使用此工作流，不需要额外启用参数：

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书，字段名为title、price，分别为完整书名、价格" --spec-output output/books-agent.json --output output/books.json
```

可以用 `--fields title price --count 25` 固定约束，用 `--max-repairs 0` 禁用修复。每次失败的具体反馈保存在最终 JSON 的 `feedback` 和 `validation_history` 中。

## 流程

1. 从原始任务建立 `TaskRequirements`：必需字段、显式请求数量和显式不支持的能力。命令行的 `--fields` / `--count` 优先；明确写出的字段标识符和常见数量表达直接解析，其他自然语言最多调用一次 LLM，且不向该步骤提供候选规则。
2. 将约束交给单 browser-use Agent。成功时仍提交原有 ScraperSpec；登录等人工操作交给 `wait_for_user`，用户继续后在同一浏览器恢复；发现复杂虚拟列表等不支持能力时调用 `report_unsupported`，立即结束。
3. 独立检查规则：所有必需字段必须存在并标记 required；显式数量必须等于 max_items。沿用 Pydantic 协议校验，不扩展 ScraperSpec，不允许脚本。
4. 使用现有 Executor 试跑候选规则，默认最多 100 条、5 页；导航超时沿用规则设置。检查提取异常、空结果、逐条缺失/空字段、数量不足和分页循环。
5. 可恢复的问题把错误码、字段、原因和上一份规则反馈给同一个 Agent 工作流，再生成并重新验证。默认最多修复两次，即总共最多三份候选规则；不会重新提取或放宽原始需求。不支持的能力、预算不足、需求解析失败、验证页数预算不足直接停止。
6. 如要求数据输出且试跑未覆盖完整任务，执行原规则并校验全量结果。试跑已覆盖时直接复用数据。所有检查通过后才保存正式规则和数据文件；运行时导航/内容错误会单独保存一份明确标记为未验证的候选规则。

规则文件仍是纯 ScraperSpec JSON，可以用原 `autoscraper` CLI 单独重跑。原 CLI 只执行规则，无法根据原始自然语言判断任务是否完成；这项判断属于 Agent 工作流。

## 状态与退出码

| status | 含义 | 退出码 |
| --- | --- | --- |
| `success` | 规则与完整输出数据通过检查 | 0 |
| `validated` | 不导出数据，只生成已试跑验证的规则 | 0 |
| `unsupported` | 当前协议无法满足必需字段/能力 | 3 |
| `unfulfilled` | 验证不通过、修复耗尽或预算/模型错误 | 1 |
| `needs_clarification` | 等待用户补充任务意图，没有默认选择 | 4 |
| `needs_user` | 需要用户在可见浏览器中操作，当前无法交互 | 5 |
| `cancelled` | 用户取消人工接管 | 130 |

内部状态使用 `workflow/outcome.py` 的 `TaskStatus` 和 `TaskStage` 类型，分别约束
任务结果及 `exploration` / `validation` / `execution` 阶段；执行器的停止原因使用
`scraping/executor.py` 的 `ScrapeStopReason`。这些 `Literal` 类型供编辑器和静态类型
检查使用，不增加运行时校验，也不改变 JSON 字段、字符串取值、退出码或完成条件。
停止原因的空字符串仍是原有初始值；`failure_stage` 未标记时仍为 `null`。

`spec_valid` 仅表示存在通过 Pydantic 的规则；`execution_success` 表示执行器返回且无提取错误；`task_fulfilled` 还要求需求和数据检查通过，并且不是部分样本。三者可能不同。

只生成规则时，若请求超过试跑范围，返回 `validated`、`sample_only: true`、`task_fulfilled: false`，明确表示只验证了样本。若试跑页数预算甚至不足以取得预期样本，返回 `validation_limit`，可提高 `--validation-pages` 后重跑。需要全量验证时提供 `--output`。

## 反馈和边界

可恢复反馈包括 `missing_required_field`、`count_mismatch`、`invalid_spec`、`selector_failure`、`empty_extraction`、`missing_data`、`insufficient_items`。不可恢复反馈包括 `content_timeout`、`navigation_timeout`、`navigation_failure`、`unsupported_capability`、`validation_limit`、`invalid_requirements`、`agent_failure`、`budget_exhausted`。

列表、分页和详情页导航超时报告 `navigation_timeout`；代理/DNS/连接故障及 HTTP
错误状态（>=400）报告 `navigation_failure`。等待列表或详情必填元素出现超时报告
`content_timeout`。均保留目标 URL 和原始错误，返回 `unfulfilled`，不交给 Agent Repair，
不自动重试。详情页 `on_item_error=skip` 仍保留类型，避免因缺少记录触发规则修复。
CSS 语法错误、多匹配字段和未发生等待超时的必填字段缺失等规则问题仍可进入有限修复。

HTTP 错误文档可能通过 JavaScript 跳转到正常 SPA 入口。执行器在原导航预算内，
最多等待 1 秒让错误页发起主文档跳转；跳转开始后，其加载及后续跳转仍共用原超时。
最终主文档须正常响应并完成 DOMContentLoaded 才继续提取。没有跳转或跳转后仍是 HTTP
错误时照常失败；iframe/资源请求成功不会掩盖主文档错误，也不会重新发送原请求。

规则已生成但验证或全量执行遇到上述运行时错误时，最终 JSON 使用
`agent_status: "completed"` 和 `failure_stage` 区分生成与失败阶段，并在
`unvalidated_spec` 给出未验证规则副本。该文件使用独立文件名并避免覆盖已有文件；
正式 `--spec-output` 和数据输出保持原样。独立 Executor 可使用
`--navigation-timeout-ms` 覆盖规则导航超时后重试，仍不自动增加 Agent 调用。

验证的导航及其他操作超时默认沿用规则 `timeout_ms`。显式指定
`--validation-timeout-ms` 时取两者较小值（上限 30000 毫秒）。内容等待独立使用
`--content-timeout-ms`（默认 30000 毫秒），保存到候选规则，不受导航超时截断。
旧规则不设置 `content_timeout_ms` 时沿用原 `timeout_ms`。不自动延长等待。

语义等待通过独立的 LLM 实例判断就绪，默认整次任务最多 8 次（`--max-wait-llm-calls`），
修复不重置，调用量单独报告为 `wait_llm_calls`。工具限制 DOM 检查次数和间隔，
DOM 摘要不变时跳过模型调用；LLM 失败报告 `llm_error`，不冒充内容超时。

需求提取、探索及修复共享一个 LLM 实例和 `--max-steps` 调用总预算，修复不重置预算。`--timeout` 限制剩余探索时间；同步试跑和批量抓取使用各自的 Executor 限制，不由异步取消强行中断。浏览器清理保留原有的额外 20 秒上限。

主 Agent 可通过 `ask_user` 澄清影响目标的歧义。工具用终止结果结束当前动作批次，
生成器在本轮 Agent 运行结束后接收回答，再通过 browser-use 的 `add_new_task` 保留
原浏览器和历史继续；人工等待不在 Agent 的步骤超时内，也从工作流总时间中扣除。
恢复不重置步骤/模型预算，不占用一次 Repair；问答由工作流保存，后续 Repair 重新注入。
没有回答时工作流返回 `needs_clarification`，不会试抓取、修复或发布文件，CLI 退出码 4。
显式要求不支持的能力仍可直接返回 `unsupported`，不必为了澄清而重新询问明确要求。

`clarifications` 记录 Agent 提问和用户原文回答，`pending_clarification` 保留未回答问题。
`original_url` / `actual_start_url` 用于呈现实际来源，URL 不同本身不构成审批条件。
`fulfillment_basis` 区分原任务与澄清后的任务；`task_fulfilled` 延续原有字段/数量/执行
校验语义，不证明入口等价或任意语义约束。`validation_scope` 对此做出明确说明。
提问判断及语义约束遵守依赖主 Agent，不宣称强制拦截所有入口替换。

字段校验检查名称、必填性及非空数据，不验证任意字段的业务语义。未给出精确字段名的自然语言需求依赖一次模型解释，可以通过 `--fields` / `--count` 消除这部分歧义。不把所有名为 description 的字段一概判为详情页字段；由网页实际证据决定。v2 支持一层详情提取，字段完整性检查使用列表与详情字段的并集。

验证不通过时不会写规则或数据，因此不会用不完整数据覆盖已有成功文件。文件系统 I/O 错误下的多文件事务性不属于本次验证机制。

## 测试

`tests/workflow/test_workflow.py` 使用固定模型候选和真实离线 Playwright 页面，覆盖支持任务、遗漏字段、选择器失败后的修复与重验、次数/调用预算、unsupported 立即停止、样本与全量结果区分、失败时保留文件。默认测试不调用真实 LLM。

## 验证记录（2026-10-21）

默认回归 71 项通过、2 项线上测试跳过，Ruff 检查及格式检查通过。`ScraperSpec`、Executor 和用户已修复的 Responses 适配器均未改动。

使用真实 DeepSeek 分别运行两种任务，均未提供 CSS Selector：

- title + price：6 次 LLM 调用，执行得到 25 条，status=success，退出码 0。
- issue 原始 title + price + description：4 次 LLM 调用确认 description 只存在详情页，status=unsupported，退出码 3；attempts=1，没有修复重试，规则和数据文件均不存在。

真实调用日志与结果位于 `output/validation-loop/`，该目录不纳入版本控制。Windows 实测中，unsupported 结束后出现了 asyncio 的 ignored closed-pipe 清理警告；最终状态与退出码仍为上述结果。

## v2 详情提取

详情预算与列表页预算独立。试跑通过 `--validation-detail-pages`（默认 100）限制详情导航次数，
失败导航也消耗预算。`detail_limit` 生成不可恢复的 `validation_limit`，不会被当作正常结束或任务成功。
详情明确的字段规则错误通过既有反馈进入有限修复；等待详情必填元素超时报告
不可恢复的 `content_timeout`，导航超时为 `navigation_timeout`，其他导航错误为
`navigation_failure`。失败时仍保留原有输出文件。
CLI 新增 `detail_pages` 计数，CSV 和 JSON 都导出合并字段。

上面的 2026-10-21/22 历史验证中的 description=unsupported 是 v1 行为；v2 可表达该请求。
