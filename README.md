# AutoScraper

输入网站 URL 和自然语言需求，由 browser-use Agent 探索网页并生成抓取规则，再交给 Playwright Executor 导出数据。

## 运行 Agent

以下命令以 Linux Bash 为例，需要 Python 3.12 和 uv。PowerShell 中相同的 `uv` 命令可直接使用。

### 1. 安装依赖和浏览器（首次运行）

```bash
cd /path/to/AutoScraper
uv sync --extra agent --locked
uv run --extra agent playwright install chromium
```

如果在 Ubuntu 运行，由于 AppArmor 安全策略阻止了 Playwright 自带的 Chromium 创建沙箱，需要给 Chromium 添加 AppArmor 规则

```bash
sudo tee /etc/apparmor.d/playwright-chromium > /dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile playwright-chromium /home/cloverfrog/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome flags=(unconfined) {
  userns,
}
EOF

sudo apparmor_parser -r /etc/apparmor.d/playwright-chromium
```

### 2. 配置 DeepSeek 和浏览器（每个新终端执行）

```bash
export DEEPSEEK_API_KEY="替换为你的 DeepSeek API Key"
export AUTOSCRAPER_BROWSER_PATH="$(uv run --extra agent python -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
```

PowerShell 对应命令：

```powershell
$env:DEEPSEEK_API_KEY = "替换为你的 DeepSeek API Key"
$env:AUTOSCRAPER_BROWSER_PATH = uv run --extra agent python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
```

如果终端已经配置 `DEEPSEEK_API_KEY`，无需再次赋值。默认接口为 `https://api.deepseek.com`，模型为 `deepseek-flash`；Agent 通过 DeepSeek Responses API 的 JSON Schema 模式生成结构化动作。

### 3. 启动 Agent，生成规则并抓取数据

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书，字段名为title、price、url，分别为完整书名、价格、绝对详情链接" --spec-output output/books-agent.json --output output/books.json --output output/books.csv --headed
```

`--headed` 显示探索、试抓取和正式抓取的浏览器；去掉后所有阶段均使用无头模式。
`--browser-path` 或 `AUTOSCRAPER_BROWSER_PATH` 指定的可执行文件也用于所有阶段。
此命令会调用 LLM，产生 API 费用。

规则会先校验必需字段、数量并试跑提取；可恢复的错误默认最多修复 2 次。验证通过后得到：

| 文件 | 内容 |
| --- | --- |
| `output/books-agent.json` | Agent 生成的可复用抓取规则 |
| `output/books.json` | 抓取数据，JSON 格式 |
| `output/books.csv` | 抓取数据，CSV 格式 |

换网站时，替换命令里的 URL 和自然语言需求即可。

列表字段和一层详情页字段均可抓取；登录、复杂虚拟列表等超出当前协议的场景，会返回 `status: "unsupported"` 并以退出码 3 结束，不再静默省略字段。其他验证失败返回 `unfulfilled` / 退出码 1，保留已有规则和数据文件。运行时导航或内容超时时，还会另存未验证的候选规则。具体反馈见终端最后一条 JSON。

需要精确固定字段和数量时，可在上述命令中加 `--fields title price --count 25`。否则会先独立解析自然语言需求，再生成和验证规则。

## 只生成规则 / 重复抓取

**生成并验证规则**：不传 `--output`。仍会执行有上限的试抓取，最后返回 `validated`；若只验证了部分记录，`sample_only: true`，不会声称已完成全部数量。

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书的完整书名、价格和详情链接，需要分页" --spec-output output/books-agent.json
```

**用已有规则抓取**：运行 Executor，不调用 LLM。

```bash
uv run autoscraper output/books-agent.json --output output/books.json --output output/books.csv
```

独立执行器也默认无头运行，可加 `--headed` 显示浏览器，并通过 `--browser-path` 指定
可执行文件；未传路径时读取 `AUTOSCRAPER_BROWSER_PATH`，再回退到 Playwright 默认浏览器。
这些启动选项不保存到规则 JSON 中。

也可直接使用仓库中已验证的 Agent 规则快照：

```bash
uv run autoscraper examples/books.agent-generated.json --output output/books.json
```

## 详情页补充字段

要求抓取 `title`、`price`、`description` 时，Agent 可探索列表及详情页，生成 v2 规则。
执行器使用独立详情 Page 串行补充字段，继续沿用原有需求校验和有限修复流程。

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书，字段名为title、price、description，分别为完整书名、价格和详情描述" --spec-output output/books-detail-agent.json --output output/books-detail.json --output output/books-detail.csv
```

也可直接运行手写示例（不调用 LLM）：

```bash
uv run autoscraper examples/books.detail.json --output output/books-detail.json --output output/books-detail.csv
```

v1 规则继续原样运行。v2 的 `detail.url_selector` 从每条列表记录取得一个 `href`；
`detail.fields` 相对于详情页解析，不能与列表字段重名。顶层 `max_pages` 限制列表页，
`detail.max_pages` 独立限制详情导航次数，失败也计入；达到详情预算返回 `detail_limit`，
Agent 工作流判定为验证失败，不覆盖已有输出。只运行 Executor 时可导出部分数据，退出码为 2。
完整语义见 [协议参考](docs/reference.md)。

## 常用配置

| 参数 | 用途 | 默认值 |
| --- | --- | --- |
| `--spec-output` | 规则输出路径，必填 | — |
| `--output` | 数据输出路径，可重复指定 CSV / JSON | 不抓取数据 |
| `--headed` | 显示探索、试抓取和正式抓取浏览器 | 无头模式 |
| `--model` | LLM 模型名 | `deepseek-flash` |
| `--base-url` | 模型接口地址 | `https://api.deepseek.com` |
| `--max-steps` | 需求解析、探索、修复共享的 LLM 调用上限 | 12 |
| `--timeout` | 探索总超时，秒 | 240 |
| `--llm-timeout` | 单次 LLM 超时，秒 | 45 |
| `--content-timeout-ms` | 探索、验证和执行的内容等待上限（毫秒，1–120000） | 30000 |
| `--max-wait-llm-calls` | 整次任务语义等待的独立 LLM 调用上限（0–30） | 8 |
| `--max-output-tokens` | 单次 LLM 输出 token 上限 | 3000 |
| `--browser-path` | Chromium / Chrome 可执行文件路径 | 读取环境配置或自动查找 |
| `--structured-output` | 保留的兼容参数 | Responses 适配器始终使用 JSON Schema |
| `--fields` | 明确必需字段名，例如 `--fields title price` | 从任务独立解析 |
| `--count` | 明确请求记录数 | 从任务独立解析 |
| `--max-repairs` | 验证失败后的最大修复次数，0–3 | 2 |
| `--validation-items` | 试抓取最多记录数 | 100 |
| `--validation-pages` | 试抓取最多列表页数 | 5 |
| `--validation-detail-pages` | 试抓取最多详情导航次数（含失败） | 100 |
| `--validation-timeout-ms` | 可选：缩短试抓取导航及其他操作超时，毫秒（最高 30000）；不截断内容等待 | 沿用规则 `timeout_ms` |

其他兼容 Responses API 的接口可设置 `AUTOSCRAPER_API_KEY`、`AUTOSCRAPER_BASE_URL` 和 `AUTOSCRAPER_MODEL`。密钥优先读取 `AUTOSCRAPER_API_KEY`，其次读取 `DEEPSEEK_API_KEY`；配置不自动加载 `.env`。

完整参数：`uv run --extra agent autoscraper-agent --help`。

Agent 执行轨迹默认保存至 `.agent-runtime/runs/<开始时间>_<run_id>/`，按名称即可排序，包括持续追加的事件、经过
筛选的 History 和最终汇总。可用 `--runs-dir` 更改路径。默认保留完整 URL、决策摘要、
工具反馈、问答和失败原因，仅替换明确凭据；完整 DOM 和模型请求/响应不默认采集。
不再创建重复的 console.log。日志失败会警告并继续任务，不改变 stdout JSON 与退出码。
格式、指标、记录策略及 Python 接口见 [执行追踪](docs/tracing.md)。

## 任务需求澄清

当不同的合理理解会实质改变数据来源、范围、筛选条件等目标，且已有上下文无法判断
用户意图时，Agent 可以调用 `ask_user`。例如原入口依赖当前不支持的复杂虚拟列表，
但另一个传统分页入口可能提供所需数据：Agent 应先说明页面证据、替代方案及数据/顺序
尚未证明等价，再询问用户。普通导航、重定向、分页和详情访问不因 URL 不同就触发提问。

在交互终端中，问题、说明和可选建议显示在 stderr；可输入选项编号或自由回答。
提问结束当前动作批次，回答后保留浏览器和探索历史继续执行；修复轮次也保留问答。
人工回答的等待时间不计入 `--timeout`，模型调用及步骤预算不会重置。
回答补充任务意图，不绕过协议能力边界，也不自动放宽原有字段/数量约束。

留空、输入结束、中断回答、非交互 stdin 或使用 `--non-interactive` 时，不默认选择，
返回 `status: "needs_clarification"` 和退出码 **4**。最终 JSON 的 `pending_clarification`
包含 `question`、`explanation`、`options`；已有规则和数据文件保持原样。
第一版只支持同一次进程运行中的交互继续，不提供跨进程保存/恢复；退出后可将需求
补充到原任务中重新运行。

最终 JSON 还包含 `original_url`、`actual_start_url`、`clarifications` 和
`fulfillment_basis`（`original_task` / `clarified_task`）。`validation_scope` 明确说明
独立验证覆盖协议、必需字段、数量和实际提取；用户允许替代入口不代表内容或顺序等价
已获证明。语义约束依赖主 Agent 遵守，程序不会仅凭 URL 差异判断违规；主 Agent
仍可能漏判需要澄清的情形。这是需求澄清能力，不是强制操作审批或通用语义验证系统。

## 浏览器与缓存路径

如果之前使用了项目内的 `.browsers`，在上面的安装、浏览器路径获取和运行命令**之前**执行：

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"
export UV_CACHE_DIR="$PWD/.uv-cache"
```

PowerShell 对应命令：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.browsers"
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
```

这两个变量只在当前终端有效，新终端需重新设置。也可直接用 `--browser-path` 指定本机 Chrome 路径。

## 测试

默认测试不调用真实 LLM：

测试中的默认运行日志自动写入 pytest 临时目录，不会混入项目的
`.agent-runtime/runs/`；显式指定的其他日志目录仍按用例配置生效。

```bash
uv run --extra agent pytest
```

付费 LLM 测试单独启用，需要前面的密钥和浏览器配置：

```bash
export RUN_LLM="1"
uv run --extra agent pytest -m llm
```

PowerShell 对应命令：

```powershell
$env:RUN_LLM = "1"
uv run --extra agent pytest -m llm
```

原 Executor 的线上测试用 `RUN_LIVE=1` 和 `pytest -m live` 单独启用。受限 Windows 环境可以追加 `--basetemp .pytest-tmp-local`。

真实 DeepSeek 修复测试默认跳过，需单独启用（会产生 API 费用）：

```bash
RUN_LIVE_REPAIR=1 uv run --extra agent pytest tests/workflow/test_live_repair.py -v
```

PowerShell 先执行 `$env:RUN_LIVE_REPAIR = "1"`，再运行上述 pytest 命令。
测试使用已配置的密钥和 DeepSeek 模型，固定访问真实 DeepSeek 接口；第一次生成规则后故意破坏 `price` 选择器，
由原工作流的 Validator 产生反馈，再将失败规则和反馈传入真实生成器，仅允许一次修复。
最终通过 Playwright 检查跨两页的 25 条书籍数据。候选规则、损坏规则和验证报告保存在 pytest 临时目录中。

## 项目结构与当前范围

源码按职责分组，包根目录只保留公共入口 `__init__.py`：

```text
src/autoscraper/
├── agent/          # 浏览器探索、工具、模型适配和需求澄清
│   ├── explorer.py
│   ├── tools.py
│   ├── dom.py
│   ├── waiting.py
│   ├── llm.py
│   ├── responses.py
│   └── clarification.py
├── scraping/       # 抓取协议、确定性执行和数据导出
│   ├── spec.py
│   ├── executor.py
│   └── export.py
├── workflow/       # 任务编排与独立验证
│   ├── runner.py
│   ├── requirements.py
│   ├── outcome.py
│   └── validation.py
├── cli/            # 两个命令行入口
│   ├── scraper.py
│   └── agent.py
└── observability/  # 执行追踪与日志
    └── tracing.py
```

测试分别放在 `tests/agent/`、`tests/scraping/`、`tests/workflow/` 和
`tests/observability/`，运行 `pytest` 仍会统一发现全部测试。
CLI 命令名称保持为 `autoscraper` 和 `autoscraper-agent`。

```text
自然语言需求 → 独立需求约束 → Agent 生成规则 → 验证 / 反馈修复 → Executor → CSV / JSON
```

Agent 负责探索和生成规则，Executor 只负责执行。当前支持列表与一层详情页字段、文本/属性提取、绝对 URL、Next 链接分页、数量限制和去重，不接受模型生成的 Python/JavaScript 脚本。


模块边界、协议语义、工具限制和验证记录见 [协议与实现参考](docs/reference.md)。

字段遗漏防护、结果状态和修复策略见 [Validation + Feedback Repair Loop](docs/validation-loop.md)。

## 动态内容等待

延迟渲染页面可以显式配置内容等待时间：

```bash
uv run --extra agent autoscraper-agent "https://quotes.toscrape.com/js-delayed/" "抓取前25句语录，字段名为quote、author" --spec-output output/quotes.json --content-timeout-ms 30000 --max-wait-llm-calls 8
```

探索时，已知 CSS 使用 `wait_for_selector`，未知结构使用 `wait_for_content`，由 Agent 指定
`success_criteria`、`interval_seconds` 和 `max_checks`。工具一次调用内完成有界等待，
返回 `ready` / `timeout` 和当前 DOM。语义判断在 DOM 摘要变化后才调用模型；
检查次数包含跳过判断的检查，仍受时间上限约束。语义等待额外产生 API 费用，独立计入
最终 JSON 的 `wait_llm_calls`，所有修复轮次共用 `--max-wait-llm-calls`；
主 Agent 的 `--max-steps` 预算保持独立。设为 0 可禁用语义判断调用。

内容超时保存为规则中的 `content_timeout_ms`。验证时，内容等待使用这一配置，导航默认使用规则的 `timeout_ms`，两者互不截断。
重复抓取可用 `uv run autoscraper output/quotes.json --output output/quotes-data.json --content-timeout-ms 45000`
覆盖此配置；旧规则未设置该字段时沿用原 `timeout_ms`。

导航等待超时为 `navigation_timeout`，等待内容出现超时为 `content_timeout`，均返回明确失败，
不自动重试或触发 Agent Repair。代理、DNS、HTTP 等导航失败仍为 `navigation_failure`；
明确的 CSS/字段规则问题继续使用 `selector_failure`。语义就绪判断只辅助探索，不能代替字段验证。

如果 Agent 已生成规则，但验证或全量执行因导航/内容运行时错误失败，最终 JSON 会报告
`agent_status: "completed"`、`failure_stage: "validation"` 或 `"execution"`，并在
`unvalidated_spec` 给出单独保存的规则路径。例如请求 `--spec-output output/quotes.json`
时，第一份候选规则保存为 `output/quotes.unvalidated.json`。它尚未通过数据验证，任务仍以
退出码 1 结束；已有的 `output/quotes.json` 和数据文件不会被覆盖。再次失败会生成
`quotes.unvalidated-2.json`，也不会覆盖前一份候选规则。

网络恢复后，可直接重试候选规则，不必再次调用 Agent。若这次导航确实需要更久，
可在独立 Executor 中临时覆盖导航超时（最高 120000 毫秒）：

```bash
uv run autoscraper output/quotes.unvalidated.json --output output/quotes-data.json --navigation-timeout-ms 60000
```

## 动态分页

`pagination` 必须显式声明 `type`；旧的仅含 `next_selector` 的规则需迁移，
例如 `{"type":"link","next_selector":"li.next a"}`。协议版本 1/2 的列表/详情含义不变。

Load More 使用 `{"type":"click","next_selector":"button.more","mode":"append"}`；
同 URL 替换列表使用 `mode: "replace"`，执行器比较声明的列表字段值确认更新。
无限滚动使用 `{"type":"scroll","max_scrolls":100,"no_change_limit":3}`，
可通过 `container_selector` 指定唯一的滚动容器。虚拟列表暂不支持。

click/scroll 可声明站点真实存在的 `loading_selector` 和 `end_selector`，均按可见状态判断。
追加列表以记录数增长确认推进；加载标志消失不等于结束。
`max_pages` 统计已处理列表批次（含初始批次），`max_scrolls` 独立限制滚动动作总数。
每次等待受 `content_timeout_ms`（默认 `timeout_ms`）限制。

停止原因：`no_next` 为按钮消失/禁用，`end_marker` 为显式结束；
`no_more_content` 是到底后连续多轮无新增的启发式结束，可能遗漏延迟很长的数据。
`scroll_limit` / `max_pages` 是预算截断，不能作为完整完成。
仍可用的按钮没有更新记录会报告 `content_timeout`，点击/滚动动作失败会报告
`pagination_action_failure`。数量不足始终另由验证器判定。

Agent 开放内置 click/scroll，并通过 `explore_scroll` 在一次调用中执行多轮有界滚动、
等待和记录数检查，返回滚动次数、初始/最终记录数及停止原因；普通页面滚动复用内置动作。
执行器始终使用 Playwright，不在批量抓取时调用 LLM。

执行器的页面滚动使用 CDP 模拟手势，指定容器使用滚轮输入，每次动作后短暂停顿。
尚未到底且未观察到加载时继续推进，不会每滚一屏就等满内容超时；到底或观察到加载后，
才等待记录更新。`pagination.scroll` 事件记录滚动前后位置、记录数及移动/等待阶段。

Agent 对已知选择器的数量等待使用 `wait_for_selector(selector, min_count=N+1)`：
例如 Load More 点击前已有 6 条，就等待至少 7 条。工具直接返回 `initial_count`、`count`、
`min_count` 和状态，不调用 LLM，不受 DOM 摘要截断影响。默认 `min_count=1` 仍等待单个节点。
正常数量超时保留实际计数；若连接或浏览器操作整体超时无法读取数量，则计数返回 null。
替换型列表仍需检查字段内容变化，不能以数量增长作为通用推进条件。
