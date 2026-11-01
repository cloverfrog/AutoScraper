# 协议与实现参考

返回 [运行指南](../README.md)。

## 模块边界

```text
agent/explorer.py → scraping/spec.py → scraping/executor.py → records → scraping/export.py
```

- `scraping/spec.py` 仅依赖 Pydantic；通过 `ScraperSpec.model_validate_json(...)` 验证规则，通过 `ScraperSpec.model_json_schema()` 提供 Agent 使用的 JSON Schema。
- `scraping/executor.py` 接受已验证的规则，管理浏览器生命周期；`run_page` 可注入调用方管理的 Playwright Page，方便离线测试和后续集成。
- `ScraperExecutor(headless=True, executable_path=None)` 单独接收浏览器启动选项；Agent 工作流将探索设置传给试抓取和完整执行。独立 CLI 同样支持 `--headed`、`--browser-path` 和 `AUTOSCRAPER_BROWSER_PATH`。未指定路径时使用 Playwright 默认浏览器，运行选项不写入抓取协议。
- `scraping/export.py` 处理 UTF-8 CSV/JSON，空 CSV 也保留字段表头；`cli/scraper.py` 负责文件读取、调用与退出状态。
- `examples/books.json` 是手写规则；`tests/scraping/test_scraper.py` 用 Playwright 路由提供离线 HTML，并提供可选线上测试。
- `agent/explorer.py` 编排探索、超时、输出解析和规则保存；`agent/tools.py` 使用 browser-use 扩展接口提供 DOM 检查与选择器测试；`cli/agent.py` 提供独立入口。Agent 不导入 Executor，CLI 通过工作流协调探索与执行。
- `agent/dom.py` 保存工具与等待器共用的固定页面脚本；`agent/waiting.py` 负责 CSS 等待和语义就绪判断；`agent/llm.py` 创建模型并维护调用预算。工具注册不负责创建模型，等待器也不重置任务级预算。
- `workflow/requirements.py` 定义 `TaskRequirements` 并独立解析用户的字段和数量约束；`workflow/validation.py` 引用需求模型，定义校验反馈、验证配置及确定性规则/数据检查；`workflow/outcome.py` 保存任务结果并生成 CLI/追踪共用的摘要；`workflow/runner.py` 负责预算、阶段编排和反馈修复。只有工作流验证通过，CLI 才写规则和数据。`generate_spec` 是候选规则生成接口，本身不承诺任务已完成。
- `observability/tracing.py` 统一处理事件、模型/工具指标、框架日志和执行追踪文件。
- 应用内 `ChatOpenAIResponses` 与 browser-use `ChatOpenAI` 同级，通过 OpenAI-compatible Responses API 提交完整消息，并用 `text.format=json_schema` 校验每一步 `AgentOutput`。`LimitedChatOpenAI` 仅继承它并增加付费调用预算；DeepSeek 的模型、Base URL和非思考模式都由构造参数传入，不存在 DeepSeek 专属 provider 类。browser-use 同时使用精简的 flash prompt/schema，避免 `deepseek-flash` 输出内部 DSML 工具语法。


## 探索工具与限制

基础工具包含 `navigate`、结构化 `done`、`click`、`scroll`、`explore_scroll`、`inspect_page`、`test_selector`、`wait_for_selector`、`wait_for_content`。探索器还提供 `wait_for_user`，将登录等浏览器操作交给用户，继续后重新观察页面。验证工作流额外提供 `report_unsupported`，将能力限制作为失败终态提交，不生成部分规则。前两者使用 browser-use 内置实现；DOM 工具使用当前 `BrowserSession` 的页面接口。`inspect_page` 输出 DOM 标签、class/id、链接属性与有限文本，支持 CSS 根节点缩小范围；`test_selector` 返回匹配数量和前 5 项样本，支持相对字段选择器与属性提取。

`wait_for_selector` 通过 Playwright CDP 连接定位当前 target，等待至少 `min_count` 个 CSS 元素 attached（默认 1），
返回 `initial_count`、`count`、数量门槛和状态；默认单节点等待还返回 DOM。
已知选择器的 Load More 场景，点击前有 N 条时传 `min_count=N+1`，直接检查数量增长，
不调用语义模型。正常数量超时返回实际计数；连接/操作整体超时无法获取计数时返回 null。
结束仅断开自己的连接，不关闭 Agent 浏览器。`wait_for_content` 接受
语义条件（最多 2000 字符）、间隔（0.1–10 秒）、检查次数（1–30），立即检查并在 DOM
摘要变化时独立调用 LLM。所有等待受 `--content-timeout-ms`（默认 30000，最高 120000）
及探索剩余时间限制。检查次数包含未变化的 DOM；语义调用整次任务最多 8 次，可用
`--max-wait-llm-calls` 设置 0–30。返回 ready/timeout、DOM、耗时；语义工具还报告
检查和调用次数，预算不足和模型错误分别返回 budget_exhausted / llm_error。

DOM 工具使用应用内固定、只读 JavaScript；模型只能传入经 JSON 序列化的选择器和属性参数，不能提供可执行源码。browser-use 的 evaluate、文件操作、表单输入等其余工具不对模型开放。探索提示要求检查实际 DOM、测试字段和 Next，分页时到下一页验证后提交规则。程序不包含 Books 专用选择器，也不会读取手写示例作为 Agent 输入。

`test_selector` 原样回显 `item_selector`、`field_selector` 和 `attribute`，保留
`url/count/samples` 及每条样本的 `matches/values`。`count` 始终表示记录总数；记录 CSS
非法时无法计数，返回 `null`。字段 CSS 在记录零匹配时也会检查语法。

| 状态 | 含义 |
| --- | --- |
| `invalid_css` | 非法 CSS；`error_parameter` 指明 `item_selector` 或 `field_selector`，`message` 提供浏览器错误 |
| `no_items` | CSS 合法，但没有匹配记录 |
| `sample_passed` | 已检查样本的匹配数量符合提取方式；单值要求唯一，texts/count 允许多匹配，必填值另行检查 |
| `sample_failed` | 至少一条样本字段零匹配或多匹配 |

每条样本的 `status` 为 `no_match`、`single_match` 或 `multiple_matches`，其 `matches`
是该记录内的字段匹配数，与顶层记录数无关。未传字段选择器时直接提取记录本身。
最多检查前 5 条记录，单值提取每条最多返回 2 个值，`texts` 返回样本内全部匹配文本，
`count` 返回匹配数量，每个文本值最多 300 字符；`tested_count` 和
`all_items_tested` 说明覆盖范围。`sample_passed` 只检查匹配数量，不保证属性存在、值非空
或完整规则通过验证；仍须查看 `values`（缺失属性为 `null`）并执行确定性验证。
浏览器连接、页面执行等其他异常继续沿用工具运行时错误流程，不误报为非法 CSS。
Agent 修改选择器后须原样重测，不能将失败或未经测试的选择器称为已验证。

提示词按职责维护：`build_task` 集中描述探索流程、协议语义与完成条件；工具描述解释
各自参数和返回值；`generate_spec` 仅追加本轮需求、失败候选、修复反馈与等待预算。
完整 JSON Schema 由 browser-use 的 `output_model_schema=ScraperSpec` 自动附加，同时用于
结构化 `done`，不在主提示词重复序列化。升级 browser-use 时应验证这一契约，避免 Schema
遗漏或重复。需求解析和语义就绪判断各有独立职责，不混入探索提示词。

| 控制 | 默认值 |
| --- | --- |
| 最大探索步数 `--max-steps` | 12，允许 1–50；主流程 LLM 调用数同样不超过该值，语义等待单独限额 |
| 探索总超时 `--timeout` | 240 秒，浏览器清理额外最多 20 秒 |
| 单次 LLM 超时 `--llm-timeout` | 45 秒 |
| 单次输出上限 `--max-output-tokens` | 3000 token |
| 每步工具动作 | 最多 3 个 |
| 错误与辅助调用 | SDK 重试为 0；结构化输出解析/校验失败最多额外生成 2 次，每次计入调用预算；连续失败上限 1；关闭 judge、planning、compaction 及失败后的最终 LLM 调用 |

结构化输出重试在同一模型调用内使用相同消息和 Schema，不重复浏览器操作；空输出也按
JSON 校验失败处理。网络、限流、服务端失败和明确标记的不完整响应不触发此重试。
预算耗尽、原有调用/任务超时或取消均可提前结束重试，不额外延长超时。

DOM 样本、历史窗口与输入任务长度也有限制。计费由接口提供方决定，这些限制控制调用次数、输出规模和时间，不承诺固定金额或精确输入 token 上限。需求解析和所有探索/修复共用调用上限与探索时间预算；试抓取有独立的数量、页数和操作超时限制，批量执行仍遵守 ScraperSpec 的限制。验证失败最多修复 2 次，不支持的能力直接停止。未显式启用时关闭 browser-use 遥测与云同步。详见 [验证循环](validation-loop.md)。


## 规则语义

可选的顶层 `content_timeout_ms`（1–120000 毫秒）限制每次等待内容出现的时间；
未设置时沿用 `timeout_ms`，保持旧规则行为。Agent CLI 始终将用户的内容等待配置
写入规则，并在试跑与全量执行时保留，不受验证导航超时截断。
独立 Executor CLI 可用 `--navigation-timeout-ms`（1–120000 毫秒）临时覆盖规则的
`timeout_ms`；`--content-timeout-ms` 只覆盖内容等待。运行时验证失败保留的
`*.unvalidated.json` 可直接交给该 CLI 重试，但文件名表示它还没有通过数据验证。

参考完整示例 `examples/books.json`：`item_selector` 匹配每条列表记录；`fields` 中每个 CSS Selector 相对于该记录解析。列表字段与详情字段均支持以下 `extract`：

| 方式 | 输出与匹配规则 |
| --- | --- |
| `text`（默认） | 单元素文本字符串，多匹配报错 |
| `attribute` | 单元素属性字符串，必须指定 `attribute`，多匹配报错 |
| `texts` | 所有匹配元素的文本列表，保留 DOM 顺序和重复项 |
| `count` | 匹配元素数量（整数），零是有效值，无需等待匹配元素出现 |

文本和属性值会去除首尾并合并连续空白；价格保持字符串，不隐式转换数值。
必填 `texts` 必须是非空列表且每项文本非空；可选 `texts` 无匹配输出 `[]`。
`texts` / `count` 不接受 `attribute` 或 `absolute_url` 转换，不提供转换流水线。
计数只反映选择器匹配数，例如固定五个星形节点不能推断实际评分，应提取表示评分的类名。

```json
{
  "tags": {"selector": ".tags a", "extract": "texts"},
  "active_stars": {"selector": ".star.active", "extract": "count"}
}
```

JSON 导出保留数组和整数；CSV 将文本列表编码为 JSON 单元格（如 `["中文", "tag"]`），计数输出数字文本。
列表参与去重时保留顺序和重复项，原始结果不变。Agent 使用 `test_fields` 检查多元素字段，
证据按页面、记录根、字段选择器、属性和提取方式隔离，单值测试不能替代 `texts` / `count` 测试。

`transform: "absolute_url"` 使用当前页面最终 URL 解析相对链接，只接受 HTTP(S)。`required` 默认 true；可选字段缺失输出 null。未知协议字段、未知版本、无效数量及无效去重字段会在执行前被拒绝。CSS 语法在浏览器执行时验证。

`pagination` 使用显式 `type` 判别：`link` / `click` / `scroll`，旧 JSON 需迁移。
`link` 的 `next_selector` 唯一匹配具有 href 的 Next 链接，保留 URL 循环检测。
`click` 的 `next_selector` 匹配按钮，`mode` 为 `append`（默认）或 `replace`；
追加模式等待记录数增加，替换模式等待声明的列表字段内容更新。
`scroll` 可声明唯一的 `container_selector`，默认滚动页面；`max_scrolls` 默认 100（1–1000），
`no_change_limit` 默认 3（2–10）。click/scroll 可声明可见的 `loading_selector` / `end_selector`。

执行器参考 browser-use：页面使用 CDP 滚动手势，CSS 容器使用滚轮输入，动作后短暂停顿。
未到底且未观察到加载时连续推进；到底或出现加载标志后使用内容等待预算。
`pagination.scroll` 追踪事件包含位置、记录数量和移动/等待阶段。
顶层 `max_pages` 限制处理的列表批次，滚动动作另受 `max_scrolls` 限制。
按钮消失/禁用为 `no_next`，结束标志为 `end_marker`；到底后连续无新增为启发式
`no_more_content`。`scroll_limit` / `max_pages` 为预算截断，不能作为工作流完成依据。
按钮仍可用却无内容更新、加载持续未完成按内容超时处理，动作失败单独报告。
少于数量上限但列表自然结束时正常完成；列表未出现仍按超时失败。

`max_items` 是每次运行成功且去重后记录数的上限，达到上限立即停止；自然结束时可少于该值，协议上限保持不变。`dedupe_by` 指定必填字段组成去重键，留空则按整条记录去重；保留首次出现的记录。

追加和滚动分页只提取尚未处理的新节点，已处理的旧前缀不会再次访问详情、记录错误或计入重复数。
新追加节点中的重复数据仍按 `dedupe_by` 判重；替换和链接分页每批重新提取。
按 `on_item_error=skip` 跳过的节点也视为已处理，不会在后续追加批次中自动重试。

缺失必填字段、单值字段多匹配或不支持的字段 URL 按 `on_item_error` 跳过并记录错误，或立即失败。列表导航错误、列表 HTTP 4xx/5xx、无效选择器、异常分页立即失败，不自动重试。结果包含记录、错误、访问页数、重复条数和停止原因。CLI 成功退出 0；跳过错误并导出部分数据退出 2；致命错误退出 1 且不导出本次部分数据。导出文件存在时会覆盖；多个导出文件不保证事务性。


## 列表字段筛选

可选 `filters` 在列表字段提取成功之后、详情链接处理之前执行，多个条件按 AND。
条件的 `field` 必须是 `fields` 中的列表字段；额外依赖字段也会随结果输出。
不配置时维持原有抓取行为。比较只使用临时值，结果保留原始提取值。

```json
{
  "filters": [{
    "field": "price",
    "transforms": [
      {"op": "regex_extract", "pattern": "\\d+(?:\\.\\d+)?"},
      {"op": "to_number"}
    ],
    "operator": "lt",
    "value": 30
  }]
}
```

原文 `£25.80` 以数值 `25.8` 比较，保存时仍为 `£25.80`。

| 操作 | 语义 |
| --- | --- |
| `eq` / `ne` | 文本精确比较（区分大小写）或数值比较，不隐式转换类型 |
| `contains` | 文本子串包含；文本列表中的完整字符串成员 |
| `lt` / `lte` / `gt` / `gte` | 数值大小比较，目标值必须是数字 |

可选 `transforms` 依序执行，最多 10 步，仅支持单值，不隐式逐元素处理列表：

- `regex_extract`：`pattern` 使用 Python 正则，取第一次匹配；`group` 默认 0（整体），可指定编号或命名组。
- `regex_replace`：`pattern` / `replacement` 使用 Python 正则及替换语法，替换全部匹配；无匹配保留原文。
- `map`：按 `values` 中的字符串键查表，值可为文本或有限数值。例如 `{"op":"map","values":{"Four":4}}`。
- `to_number`：严格解析带可选正负号、小数或指数的数字字符串，整数保持整数；已有数值保持数值。货币符号、千位分隔符需先显式去除，不接受 NaN/Infinity。

非法正则、捕获组或协议参数在解析时拒绝。字段缺失、正则未匹配、映射缺项或类型不适用等
属于“无法判断”，跳过该条并累计 `filter_unknown` / `filter_unknown_reasons`，不作为字段提取错误。
普通不匹配累计 `filtered_out`；AND 中任一条件确定不匹配即筛除，即使其他条件无法判断。
`items_extracted` 统计原始列表提取成功数（含重复项），上述诊断出现在执行器 CLI、工作流摘要和追踪事件中。

筛除和无法判断不消耗详情导航预算或 `max_items`；`max_items` 仍只统计最终保存的去重记录。
分页继续依据原始 DOM 状态。全部被筛掉不等于选择器失败，可以自然结束并返回空结果。
数量上限只用于截断，不要求凑足；不会因为当天匹配记录较少而触发询问或修改协议。
例如协议上限 100，每天只有 20、50、102 条时分别保存 20、50、100 条，协议始终保留 100。
预算截断、真实提取错误仍沿用原反馈；验证预算截断且当前结果全部被筛除时，完整执行可继续寻找后续匹配记录。

`TaskRequirements.filters` 在生成协议前独立保留用户条件；即使字段/数量齐全也进行需求解析，
调用仍受共享模型预算限制。Agent 发起 `ask_user` 并收到回答后，在同一 Agent 恢复前，使用共享主模型
根据当前需求和最新问答独立解析修订；只有用户明确授权才调整字段、数量或筛选。问题、说明和选项
本身不授权变更，允许换入口也不会自动降低数量。该调用计入原有模型次数及有效任务时间预算；
无回答、修订失败、超时或预算不足时不继续执行旧需求。当前需求共享给提交检查、执行验证和后续修复；
摘要中的 `original_requirements` 保留初始快照，`requirements` 报告当前需求，追踪记录修订前后值。
只有用户明确更改可复用上限（如从最多 100 改成最多 50）时才同步该值；
接受当次较少的结果不表示修改后续运行的上限。

Agent 在正常探索中自行判断可用的 URL 参数；URL 可实现时优先使用带参数的
`start_url`，无需额外声明或重复配置本地筛选；部分条件也可由 URL 覆盖、其余交给 `filters`。
筛选要求通过需求上下文传给 Agent，由它结合页面观察判断是否落实；验证器不强制逐项匹配筛选声明，
也不能独立证明 URL 筛选条件完整或等价。本地 `filters` 仍检查字段引用、操作符和转换参数，
并由执行器实际判断。协议合法性、必需字段和数量校验继续保留。


## v2 详情页协议

`version: "1"`（默认）继续支持原有列表规则，非空 `detail` 必须显式指定 `version: "2"`。
v2 也支持纯列表规则。旧执行器会拒绝 v2，避免忽略详情规则。
完整示例见 [`examples/books.detail.json`](../examples/books.detail.json)。

`detail` 包含以下字段，不接受脚本、递归跟进或详情分页：

| 字段 | 语义 |
| --- | --- |
| `url_selector` | 相对于列表记录，必须唯一匹配带非空 `href` 的元素 |
| `fields` | 非空字段集合，复用 FieldSpec，选择器相对于详情文档 |
| `max_pages` | 详情导航尝试上限，默认 100，范围 1–100000；失败也消耗预算 |

列表与详情字段名不允许重复；字段名不可为空白。列表字段集合仍要求非空。
详情字段默认必填；可选字段缺失返回 null。链接不会自动加入输出字段。
输出顺序为列表字段后接详情字段，CSV 与 JSON 均保留全部字段。

详情链接以列表页导航后的实际 URL 为基准解析，只接受有效 HTTP(S) URL；
详情字段的 `absolute_url` 以详情页最终 URL 为基准，包含重定向后的路径。
详情最终 URL 也校验 HTTP(S)。这不是网络隔离沙箱。

执行器保留列表 Page，在同一 BrowserContext 中创建并复用一个独立详情 Page，结束或报错时关闭它。
调用 `run_page` 执行详情规则时，须通过 `browser.new_context().new_page()` 创建列表页；
Playwright 的 `browser.new_page()` 便捷接口不允许在其隐式 Context 中新增 Page。
离线路由需注册在 Context 上，以覆盖列表和详情 Page。
详情导航遵守 `timeout_ms`；必填元素等待遵守独立 `content_timeout_ms`（未设置则沿用 `timeout_ms`）。缺链接、歧义链接、HTTP 错误、导航失败、超时、
缺失/空/多匹配字段按 `on_item_error` 跳过整条记录或立即失败。无效 CSS 是致命规则错误。
错误包含列表 URL、记录序号、已解析的详情 URL（若有）及字段错误。

去重键可引用列表或详情必填字段。键全部来自列表时，在导航前过滤已成功记录的重复项；
否则在字段合并后去重。失败记录不占用去重键。默认使用合并后的全部字段去重。

结果中 `pages_visited` 统计列表页，`detail_pages_visited` 统计详情导航尝试；CLI 对应
`pages` 和 `detail_pages`。详情预算耗尽且还需要访问详情时停止，`stop_reason=detail_limit`。
Agent 工作流产生不可恢复的 `validation_limit`，提示检查规则预算或验证预算，不自动反复修复。
独立 Executor CLI 可导出已完成记录，以退出码 2 表示部分结果。

Agent 必须检查观察到的详情链接并测试详情字段（`test_selector` 的根选择器可用 `html`）。
需求解析不再将详情提取归入不支持；Validator 检查列表与详情字段的并集和每行非空数据。
试跑最多访问 `--validation-detail-pages` 次详情（默认 100），不会更改原始规则的预算；
样本成功仍遵守原有 `sample_only` 和全量执行语义。

## 测试范围

默认测试实际启动 Chromium，但通过路由拦截所有请求，无需外网。覆盖分页、相对 URL、去重、数量与页数限制、分页循环、可选/必填字段、异常分页、HTTP 错误、协议校验和导出。线上测试断言前 100 条、5 页、URL 唯一、首本标题和价格格式。

当前不实现多 Agent、Web UI、数据库、反爬、多层详情页跟进或复杂动态页面。不接收规则提供的 Python/JavaScript；执行器不使用 eval、exec 或 page.evaluate。Agent 的 DOM 检查脚本由应用固定提供。网页自身脚本仍由正常浏览器运行；执行器不是用于不可信网络的安全沙箱。


## 本次验证（2026-10-17）

Python 3.12.10；依赖版本记录于 `uv.lock`。全套测试（包含线上测试）21 项通过，Ruff 检查与格式检查通过。手写 Books 规则实际访问 5 页，获得 100 条唯一记录，无错误、无重复，停止原因为 `max_items`。`output/books.json` 和 `output/books.csv` 均已回读确认 100 条；首本为 `A Light in the Attic`，第 100 本为 `In the Country We Love: My Family Divided`。输出目录已加入忽略规则，可随时重新生成。

受限 Windows 环境下可用 `pytest --basetemp .pytest-tmp-local` 将测试临时文件放在项目内；浏览器进程与线上访问需要环境允许。

接口参考：[Playwright Locator](https://playwright.dev/python/docs/api/class-locator)、[Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)、测试站点 [Books to Scrape](https://books.toscrape.com/)。

第二阶段接口参考：[browser-use 自定义工具](https://docs.browser-use.com/open-source/customize/tools/add)、[Agent 参数](https://docs.browser-use.com/open-source/customize/agent/all-parameters)。实际接口以 `uv.lock` 中 0.13.11 的源码为准。


## 第二阶段验证（2026-10-18）

真实 DeepSeek 调用在未输入任何 CSS Selector 的条件下，7 步完成 DOM 检查、字段测试、Next 检查和跨页验证。生成 `output/books-agent.json`，其原样快照保存在 `examples/books.agent-generated.json`，供查看或独立重跑，不作为探索输入。现有 Executor 抓取 2 页得到 25 本书的 title、price、url，零错误。随后使用原 `autoscraper` CLI 独立重跑生成的 JSON，同样得到 25 条且内容完全一致。验证输出位于 `output/books-agent-data.json`、`output/books-agent-data.csv` 和 `output/books-agent-replay.json`，可重新生成。

默认回归测试 42 项通过，2 项线上/付费测试按设计跳过；真实 LLM 闭环通过上述 CLI 单独执行验证。Ruff 检查与格式检查通过，现有 `spec.py`、`executor.py`、`cli.py` 未改动。


## v2 验证（2026-10-22）

离线回归 `uv run --extra agent pytest -q`：125 项通过，4 项线上/付费测试按配置跳过。
新增用例覆盖跨两页 25 条详情记录、相对与重定向 URL、导航和字段失败、超时、去重、
详情预算、完整字段导出及反馈修复。Ruff lint 与本次修改文件的格式检查通过。

`RUN_LIVE=1 uv run --extra agent pytest tests/scraping/test_detail.py::test_live_books_25_details -q`：
1 项通过。真实 Books to Scrape 返回 25 条唯一记录，访问 2 个列表页、25 个详情页，
标题、价格、description 均非空，零抓取错误。此验证使用手写 v2 示例，不调用真实 LLM；
Agent 的详情探索提示、结构化解析及修复链路通过确定性测试验证。

## 浏览器网络 JSON（v3）

v3 使用独立的 `network` 数据源，不能同时声明 DOM `item_selector`、`fields`、
`detail` 或顶层 `pagination`。旧 v1/v2 规则仍要求原有 CSS 字段；默认版本仍是 v1。
完整示例见 `examples/books.network.json`，本地页面由 `examples/network_demo.py` 提供。

```json
{
  "version": "3",
  "start_url": "http://127.0.0.1:8765/",
  "network": {
    "url_pattern": "http://127.0.0.1:8765/api/items",
    "method": "GET",
    "records_path": ["data", "items"],
    "fields": {
      "id": {"path": ["id"]},
      "stock": {"path": ["stock"], "required": true},
      "active": {"path": ["active"]}
    },
    "pagination": {"type": "click", "next_selector": "#next"}
  },
  "dedupe_by": ["id"],
  "max_items": 100,
  "max_pages": 5,
  "on_item_error": "fail"
}
```

- `url_pattern` 为大小写敏感的 HTTP(S) URL 模式（`*` 通配，方括号按字面匹配），
  只匹配去掉查询串和 fragment 的地址；
  `method` 精确匹配 GET/POST/PUT/PATCH/DELETE。页码与轮换 Token 不固化到规则中。
  首版不按请求体匹配，不能区分同一 URL、同一方法下不同 GraphQL 操作。
- 仅采集 Fetch/XHR；匹配响应须为 2xx，类型须为 `application/json` 或 `+json`。
  非 JSON、HTTP 错误、正文不可读/过大均显式失败，不作为空数据处理。
- `records_path` 必须指向数组。路径是最多 32 个键或非负整数下标组成的数组，
  空路径表示根值；键可包含点号。不支持 JSONPath 表达式、递归、通配或用户代码。
- 字段路径相对每条记录，`required` 默认 true。可输出字符串、有限整数/小数、布尔值、
  文本列表或可选 null；`0`、负数和 `false` 均是有效必填值。对象及复杂数组拒绝提取。
  `filters` 沿用现有比较规则，布尔字段可导出，但首版筛选条件不增加布尔比较语法。
- 沿用顶层 `max_items`、`max_pages`、`timeout_ms`、`content_timeout_ms`、`filters`、
  `dedupe_by` 和 `on_item_error`。`max_items` 限制筛选后的结果，`max_pages` 限制初始加载
  加点击/链接分页批次数；滚动仅将有新增记录的批次计入，空滚动另受 `max_scrolls` 约束。去重保留类型，`false` 与数字 `0` 不会因 Python 相等语义被合并。

网络分页放在 `network.pagination`：

- `{"type":"link","next_selector":"CSS"}`：读取唯一 Next 控件的 href 后正常导航。
- `{"type":"click","next_selector":"CSS","end_selector":"可选 CSS"}`：点击页面按钮，
  可选结束标志按可见性检查。不使用 DOM append/replace 进度。
- `{"type":"scroll","max_scrolls":100,"no_change_limit":3}`：复用 DOM 滚动协议的
  `container_selector`、`loading_selector`、`end_selector` 和预算约束；默认滚动整个页面。

每次导航/点击前均已监听，请求开始序号划分批次，点击前尚未完成的请求不能冒充点击后的响应。
统计分为 `responses`（收到响应头）、`extracted`（字段提取成功）、`added`（按去重键实际新增）。
三者分别写入 `network.batch`，不会因收到重复响应就判定成功。
被筛选排除但原始身份全新的记录仍证明数据推进，不计入最终 `max_items`。
同一批内多个已观察到的匹配响应合并处理；有新增且已观察到的请求全部完成后，
保留 100ms 静默窗口再结束本批，仍受内容超时约束。首版面向导航/按钮触发的有限请求批次；
不能保证纳入任意延迟发起的后台轮询或流式批次，请勿用它抓取这类持续更新接口。

响应缺失或正文未完成返回 `response_timeout`，不因按钮消失或禁用而改报正常结束。
字段/数组路径错误为 `json_path_failure`，收到响应且提取成功但无新增为
`network_progress_failure`，其余读取错误为 `response_failure`。
空数组只有未配置分页、Next 控件消失/禁用或结束标志可见时才可自然结束；
点击/链接分页的全重复批次即使按钮随后禁用也报告无推进。
滚动允许空批次/重复响应后有界重试；明确结束且无加载/在途请求时返回 `end_marker`。
否则，仅在到底、无加载/在途响应并经过安静观察后累计无新增次数，达到
`no_change_limit` 才返回启发式 `no_more_content`。安静观察窗口最多 250ms（受内容时限约束），
不保证覆盖任意延迟发起且未显示加载状态的后台请求。已观察到加载或在途正文的等待超时
为 `response_timeout`，不累计为自然结束；空滚动不消耗数据批次预算。
达到批次上限仍是 `max_pages`；达到动作上限为 `scroll_limit`，不能宣称完整完成。
明确结束优先于动作预算，动作预算优先于无新增启发式。
独立 CLI 对网络 `scroll_limit`/`max_pages` 返回退出码 2，并继续导出已有数据；
UI 的已有规则模式显示 `partial`，Agent 工作流仍按验证预算截断处理。旧 v1/v2 入口语义不变。
路径和推进错误可反馈修复；响应超时及读取错误不自动消耗修复模型预算。

Agent 使用同一浏览器上下文旁路观察，监听早于首次导航。发现/样本/测试工具都不发起 API。
提交需要当前会话的首次列表页提取证据；有分页时还需初始页的精确控件测试、真实浏览器动作
以及动作后新增记录的证据；滚动不要求 Next 控件证据。
现有 `explore_scroll` 增加可选 `network` 与 `dedupe_by` 参数，复用原内置滚动/容器动作，
按实际 JSON 响应提供 `responses`/`extracted`/`added` 和滚动停止原因。
内置 `scroll` 也会记录动作前请求基线供 `test_network` 检查，不把 DOM 增长写入网络证据。
提交时重新测试最终网络规则和去重键；独立 Executor 仍会重跑验证。
人工接管后清除网络证据及缓存，避免把接管前账户数据作为新证据。

缓存最多保留 40 个候选响应、单响应正文 1MB、正文累计 4MB；并发读取最多 40 个。
探索缓存满后淘汰旧候选并报告 dropped；执行批次超出响应数量上限则失败，不静默丢数据。
已知 Content-Length 过大时不读取，其他响应读取完成后检查实际解码字节数；
因此这是保留/解析上限，并非浏览器或协议传输层的严格内存上限。
只采集所需元数据和 JSON，不保存请求头、Cookie、请求正文或 HAR；返回的 URL 不含查询值。
查看样本最多四层、每层十二个键、数组两个元素、字符串一百字符、总输出 8000 字符，
并遮盖已知凭据字段；这不是任意敏感业务数据识别器。Trace 工具摘要不保留网络 JSON 样本。
