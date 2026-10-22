# 协议与实现参考

返回 [运行指南](../README.md)。

## 模块边界

```text
browser-use Agent → ScraperSpec JSON → ScraperExecutor → records → CSV / JSON
                         spec.py            executor.py               export.py
```

- `spec.py` 仅依赖 Pydantic；通过 `ScraperSpec.model_validate_json(...)` 验证规则，通过 `ScraperSpec.model_json_schema()` 提供未来 Agent 使用的 JSON Schema。
- `executor.py` 接受已验证的规则，管理浏览器生命周期；`run_page` 可注入调用方管理的 Playwright Page，方便离线测试和后续集成。
- `export.py` 处理 UTF-8 CSV/JSON，空 CSV 也保留字段表头；`cli.py` 负责文件读取、调用与退出状态。
- `examples/books.json` 是手写规则；`tests/test_scraper.py` 用 Playwright 路由提供离线 HTML，并提供可选线上测试。
- `agent.py` 编排探索、超时、输出解析和规则保存；`agent_tools.py` 使用 browser-use 扩展接口提供 DOM 检查与选择器测试；`agent_cli.py` 提供独立入口。Agent 不导入 Executor，CLI 在探索结束后选择性调用现有执行器。
- `validation.py` 定义独立需求约束、反馈及确定性规则/数据检查；`workflow.py` 负责需求解析、试跑现有 Executor、反馈修复和最终任务状态。只有工作流验证通过，CLI 才写规则和数据。`generate_spec` 是候选规则生成接口，本身不承诺任务已完成。
- 应用内 `ChatOpenAIResponses` 与 browser-use `ChatOpenAI` 同级，通过 OpenAI-compatible Responses API 提交完整消息，并用 `text.format=json_schema` 校验每一步 `AgentOutput`。`LimitedChatOpenAI` 仅继承它并增加付费调用预算；DeepSeek 的模型、Base URL和非思考模式都由构造参数传入，不存在 DeepSeek 专属 provider 类。browser-use 同时使用精简的 flash prompt/schema，避免 `deepseek-flash` 输出内部 DSML 工具语法。


## 探索工具与限制

基础工具包含 `navigate`、结构化 `done`、`inspect_page`、`test_selector`。验证工作流额外提供 `report_unsupported`，将能力限制作为失败终态提交，不生成部分规则。前两者使用 browser-use 内置实现；DOM 工具使用当前 `BrowserSession` 的页面接口。`inspect_page` 输出 DOM 标签、class/id、链接属性与有限文本，支持 CSS 根节点缩小范围；`test_selector` 返回匹配数量和前 5 项样本，支持相对字段选择器与属性提取。

DOM 工具使用应用内固定、只读 JavaScript；模型只能传入经 JSON 序列化的选择器和属性参数，不能提供可执行源码。browser-use 的 evaluate、文件操作、表单输入等其余工具不对模型开放。探索提示要求检查实际 DOM、测试字段和 Next，分页时到下一页验证后提交规则。程序不包含 Books 专用选择器，也不会读取手写示例作为 Agent 输入。

| 控制 | 默认值 |
| --- | --- |
| 最大探索步数 `--max-steps` | 12，允许 1–50；LLM 总调用数同样不超过该值 |
| 探索总超时 `--timeout` | 240 秒，浏览器清理额外最多 20 秒 |
| 单次 LLM 超时 `--llm-timeout` | 45 秒 |
| 单次输出上限 `--max-output-tokens` | 3000 token |
| 每步工具动作 | 最多 3 个 |
| 错误与辅助调用 | SDK 重试为 0，连续失败上限 1；关闭 judge、planning、compaction 及失败后的最终 LLM 调用 |

DOM 样本、历史窗口与输入任务长度也有限制。计费由接口提供方决定，这些限制控制调用次数、输出规模和时间，不承诺固定金额或精确输入 token 上限。需求解析和所有探索/修复共用调用上限与探索时间预算；试抓取有独立的数量、页数和操作超时限制，批量执行仍遵守 ScraperSpec 的限制。验证失败最多修复 2 次，不支持的能力直接停止。未显式启用时关闭 browser-use 遥测与云同步。详见 [验证循环](validation-loop.md)。


## 规则语义

参考完整示例 `examples/books.json`：`item_selector` 匹配每条列表记录；`fields` 中每个 CSS Selector 相对于该记录解析，必须至多匹配一个元素。`extract` 支持 `text` 或 `attribute`，后者必须指定 `attribute`。文本和属性值会去除首尾并合并连续空白；价格保持字符串，不隐式转换数值。

`transform: "absolute_url"` 使用当前页面最终 URL 解析相对链接，只接受 HTTP(S)。`required` 默认 true；可选字段缺失输出 null。未知协议字段、未知版本、无效数量及无效去重字段会在执行前被拒绝。CSS 语法在浏览器执行时验证。

`pagination.next_selector` 是页面范围的 Next 链接选择器。第一阶段要求唯一匹配、具有 href，按链接导航；不支持依赖 JavaScript 点击的分页或无限滚动。缺少/禁用 Next 正常结束，重复页面 URL 终止循环，`max_pages` 始终限制总访问页数。列表未出现按超时失败，以免规则失效时静默输出空数据。

`max_items` 限制成功且去重后的记录数，达到上限立即停止。`dedupe_by` 指定必填字段组成去重键，留空则按整条记录去重；保留首次出现的记录。

缺失必填字段、多匹配字段或不支持的字段 URL 按 `on_item_error` 跳过并记录错误，或立即失败。导航错误、HTTP 4xx/5xx、无效选择器、异常分页立即失败，不自动重试。结果包含记录、错误、访问页数、重复条数和停止原因。CLI 成功退出 0；跳过错误并导出部分数据退出 2；致命错误退出 1 且不导出本次部分数据。导出文件存在时会覆盖；多个导出文件不保证事务性。


## 测试范围

默认测试实际启动 Chromium，但通过路由拦截所有请求，无需外网。覆盖分页、相对 URL、去重、数量与页数限制、分页循环、可选/必填字段、异常分页、HTTP 错误、协议校验和导出。线上测试断言前 100 条、5 页、URL 唯一、首本标题和价格格式。

当前不实现多 Agent、Web UI、数据库、反爬、详情页跟进或复杂动态页面。不接收规则提供的 Python/JavaScript；执行器不使用 eval、exec 或 page.evaluate。Agent 的 DOM 检查脚本由应用固定提供。网页自身脚本仍由正常浏览器运行；执行器不是用于不可信网络的安全沙箱。


## 本次验证（2026-10-17）

Python 3.12.10；依赖版本记录于 `uv.lock`。全套测试（包含线上测试）21 项通过，Ruff 检查与格式检查通过。手写 Books 规则实际访问 5 页，获得 100 条唯一记录，无错误、无重复，停止原因为 `max_items`。`output/books.json` 和 `output/books.csv` 均已回读确认 100 条；首本为 `A Light in the Attic`，第 100 本为 `In the Country We Love: My Family Divided`。输出目录已加入忽略规则，可随时重新生成。

受限 Windows 环境下可用 `pytest --basetemp .pytest-tmp-local` 将测试临时文件放在项目内；浏览器进程与线上访问需要环境允许。

接口参考：[Playwright Locator](https://playwright.dev/python/docs/api/class-locator)、[Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)、测试站点 [Books to Scrape](https://books.toscrape.com/)。

第二阶段接口参考：[browser-use 自定义工具](https://docs.browser-use.com/open-source/customize/tools/add)、[Agent 参数](https://docs.browser-use.com/open-source/customize/agent/all-parameters)。实际接口以 `uv.lock` 中 0.13.11 的源码为准。


## 第二阶段验证（2026-10-18）

真实 DeepSeek 调用在未输入任何 CSS Selector 的条件下，7 步完成 DOM 检查、字段测试、Next 检查和跨页验证。生成 `output/books-agent.json`，其原样快照保存在 `examples/books.agent-generated.json`，供查看或独立重跑，不作为探索输入。现有 Executor 抓取 2 页得到 25 本书的 title、price、url，零错误。随后使用原 `autoscraper` CLI 独立重跑生成的 JSON，同样得到 25 条且内容完全一致。验证输出位于 `output/books-agent-data.json`、`output/books-agent-data.csv` 和 `output/books-agent-replay.json`，可重新生成。

默认回归测试 42 项通过，2 项线上/付费测试按设计跳过；真实 LLM 闭环通过上述 CLI 单独执行验证。Ruff 检查与格式检查通过，现有 `spec.py`、`executor.py`、`cli.py` 未改动。

