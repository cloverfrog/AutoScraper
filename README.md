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

列表字段和一层详情页字段均可抓取；登录可以交给用户手动完成，见下方“人工登录”。复杂虚拟列表、验证码绕过等不支持的场景会返回 `status: "unsupported"` 并以退出码 3 结束，不再静默省略字段。其他验证失败返回 `unfulfilled` / 退出码 1，保留已有规则和数据文件。运行时导航或内容超时时，还会另存未验证的候选规则。具体反馈见终端最后一条 JSON。

需要精确固定字段和数量时，可在上述命令中加 `--fields title price --count 25`。否则会先独立解析自然语言需求，再生成和验证规则。

探索时，`inspect_page` 会采样所有匹配根节点（最多 50 个），报告总数及截断情况；
无匹配时附上页面采样，并优先展示按钮和链接，便于依据实际 DOM 修正规则。
`test_fields` 可一次检查当前页的多个字段，减少逐字段调用。提交规则前，程序核对字段名、
精确选择器和测试所在 URL，避免将详情页测试当作列表证据。详情描述保留请求的
`description` 等字段名，放在 `detail.fields` 中即可。

提交时自动检查当前列表页上的最终字段、详情链接和分页控件，漏掉改后重测无需额外模型调用；
详情字段仍需对应详情页的测试证据。记录数按 URL 与记录选择器独立保存，点击/滚动前重新
计数并固定基线，字段测试、单独计数和数量等待可共同确认追加增长；替换模式仍比较字段内容。
必要时使用有界数量等待。这些检查不调用 LLM；失败会把具体反馈返回当前 Agent 会话继续修正。
提交被拒绝时保留候选和有界的选择器、详情 URL、分页推进摘要，供下一轮针对性复验。
摘要不包含字段文本样本，也不替代新浏览器会话的证据检查；记录根变化时反馈会指出此前
通过的根及受影响的证据关联，避免把已测详情笼统报成未测。字段、分页和详情检查顺序由
实际页面和依赖决定，探索优先用较小成本确认可能影响整体方案的疑点。
探索按页面组织待办，尽量完成当前页可做的检查后再切页；工具的 `page_work` 会报告本会话
的字段测试和参考候选待办，提醒详情页的 DOM 检查不等于字段验证。待办只作规划提示，
不阻止导航；证据齐备时可从当前页提交。规划复用现有 `memory`，不增加独立模型调用。
采样检查不保证字段语义或全量正确，
后续仍会独立试抓取。任务硬预算剩余不足 3 次主模型请求时不再启动新的修复浏览器，
结果中的 `repair_not_started_reason` 为 `insufficient_llm_requests`，并保留原失败原因。
这个三次请求门槛是保守的修复启动策略，不保证修复成功，也不表示三步。

Agent Step 与实际模型请求分别计量：一次 Step 可能因结构化输出重试发出多次请求，失败请求
也计入硬预算。默认每轮 12 步，整次任务最多 24 次主模型请求，其中 6 次预留给修复；
相比旧版默认 12 次请求，新的默认硬上限有所增加。需求解析与首轮探索最多共用 18 次请求，
由请求层强制限制；若需求解析耗尽这 18 次，直接报告预算不足，不启动探索。
有可恢复的提交/验证失败时，工作流才允许新建修复 Agent 使用余量；
只有额度耗尽而无可修复反馈时，报告 `no_recoverable_feedback`，不盲目重开探索。
多轮修复共用剩余硬预算，受 `--max-repairs` 和任务总时限约束，不重置调用计数。
`--max-repairs 0` 时不扣留预留额度；`--repair-reserve-calls 0` 可禁用预留。

browser-use 的 75% 步数预警已通过局部 Agent 子类关闭，最后一步仍强制 `done`，
提交证据检查与独立验证继续生效。人工澄清和接管恢复不增加预算。
结果 JSON 的 `agent_step_limit`、`max_llm_calls`、`llm_calls`、`repair_reserve_calls`
分别报告每轮 Step 上限、任务请求硬上限、已用请求数和本次有效预留量；
追踪中的 `budget.phase` 还记录当前阶段请求上限。语义等待模型继续独立计数。

**参数迁移**：旧版 `--max-steps` 也限制任务总请求数；现在它仅限制每轮 Step。
已有脚本若要保持总请求上限，应显式增加 `--max-llm-calls`，并设置小于该值的预留量。
例如总请求上限仍为 12，可使用 `--max-llm-calls 12 --repair-reserve-calls 3`。

## 只生成规则 / 重复抓取

**生成并验证规则**：不传 `--output`。仍会执行有上限的试抓取，最后返回 `validated`；若只验证了部分记录，`sample_only: true`，不会声称已完成全部数量。

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书的完整书名、价格和详情链接，需要分页" --spec-output output/books-agent.json
```

**基于已有规则继续优化**：增加 `--spec-input`，在任务中写清完整目标和修改意见。
Agent 首轮会参考旧规则，测试并复用仍有效的部分，根据当前任务调整字段、选择器、分页和详情规则，再走相同的验证与失败修复流程。

```bash
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书，字段名为title、price、url；修正价格选择器，保留完整书名和绝对详情链接" --spec-input output/books-agent.json --spec-output output/books-updated.json
```

未提供 `--spec-input` 时行为不变。输入必须是合法的 ScraperSpec JSON；当前任务需求优先于旧规则。
默认建议另存新文件；也允许输入、规则输出使用同一路径，但只有验证通过后才覆盖。
任务失败不会覆盖输入文件，保留的未验证候选会另存且避开输入路径。数据输出和认证状态路径不能与输入协议相同。
Python 接口对应 `run_task(url, task, settings, initial_spec=existing_spec)`，其中 `existing_spec` 为 `ScraperSpec` 对象，省略或传 `None` 则从零生成。

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
| `--spec-input` | 可选的已有 ScraperSpec JSON，供首轮探索参考并优化 | 从零生成 |
| `--output` | 数据输出路径，可重复指定 CSV / JSON | 不抓取数据 |
| `--headed` | 显示探索、试抓取和正式抓取浏览器 | 无头模式 |
| `--auth-state` | 显式加载/保存此网站、账号的认证状态文件 | 仅本次任务中传递 |
| `--model` | LLM 模型名 | `deepseek-flash` |
| `--base-url` | 模型接口地址 | `https://api.deepseek.com` |
| `--max-steps` | 每轮探索/修复的 Agent Step 上限（1–50） | 12 |
| `--max-llm-calls` | 需求解析、探索、修复共享的主模型请求硬上限（1–200） | 24 |
| `--repair-reserve-calls` | 首轮不可使用的修复请求预留量（0–199，必须小于硬上限） | 6 |
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
回答补充任务意图，不绕过协议能力边界。用户明确同意修改字段、数量或筛选条件后，
系统在 Agent 恢复前同步当前需求，提交与执行验证均以新需求为准；只同意换入口不会自动放宽其他约束。
数量始终表示每次运行的上限。设置 `max_items=100` 后，今天 20 条取 20 条，明天 50 条取 50 条，
后天 102 条取前 100 条；保存的协议仍保持 100，可每天复用。自然结束少于上限是正常成功，
不会因此询问、修复选择器或降低协议上限。只有用户明确修改上限本身时才更新该值。
摘要的 `original_requirements` 保留原始需求，`requirements` 保存当前需求。

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

## 人工登录

需要登录的网站使用 `--headed` 在交互终端运行。Agent 遇到登录、扫码或验证码等阻塞时，
可以调用 `wait_for_user`，暂停自动操作。用户直接在浏览器完成操作，回到终端按 **Enter**
继续，输入 `q` 或按 Ctrl+C 取消。请勿在终端输入账号、密码或验证码。

```bash
uv run --extra agent autoscraper-agent "https://example.com/reports" "抓取前20条，字段名为title、price" --headed --spec-output output/reports.json --output output/reports-data.json
```

继续后 Agent 重新观察页面并确认目标可访问，保留原任务和浏览器继续探索。
按 Enter 本身不表示认证成功；程序不会持续监听并自动接回浏览器。
人工等待不占探索超时，恢复不重置模型调用预算，也不新增一次修复。

Cookie 和 localStorage 认证状态默认只在本次任务中传递，供修复、试抓取及正式执行使用。
需要下次复用时，显式加入 `--auth-state .agent-runtime/auth/example-work.json`；
文件存在时加载，完成探索/执行后更新。不同网站/账号使用不同文件，删除文件即可清除。
状态文件与规则、数据文件必须使用不同路径。`.agent-runtime/` 已被 Git 忽略，状态文件
使用私有文件权限并原子替换；它包含敏感凭据，不应提交到仓库或分享。
本实现不记录用户键入的账号密码，也不将状态快照交给模型或追踪记录。

独立 Executor 可以复用该状态，也可以在抓取前主动暂停等待手动登录：

```bash
uv run autoscraper output/reports.json --output output/reports-data.json --auth-state .agent-runtime/auth/example-work.json
uv run autoscraper output/reports.json --output output/reports-data.json --headed --login --auth-state .agent-runtime/auth/example-work.json
```

无头模式、非交互 stdin 或 `--non-interactive` 下 Agent 遇到人工操作请求时，返回
`status: "needs_user"`、`pending_handoff` 和退出码 **5**，保留已有输出，关闭浏览器；
此时需在可见浏览器/交互终端重新运行。取消返回 `cancelled` 和退出码 **130**。

这一版支持同次进程中的人工接管，以及 Cookie/localStorage 快照复用。
sessionStorage、IndexedDB 等认证方式和抓取中途认证失效后的自动识别、断点恢复尚未支持。
执行器不会把普通内容超时直接认定为登录失效；状态过期时可用 `--headed --login` 重新登录后重跑。

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
├── browser/        # 人工接管、任务内认证状态与显式状态文件
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
主模型的 `--max-llm-calls` 请求预算保持独立。设为 0 可禁用语义判断调用。

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
`pagination_action_failure`。少于 `max_items` 但列表自然结束时正常完成，预算截断仍报告未完成。

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

动态分页在每批提取前保存记录数量（替换模式保存字段快照），提取后先核对列表变化。
详情访问等慢操作期间自动到达的新批次，会先补提取，再判断结束标志或按钮状态；
补提取仍消耗 `max_pages`，并遵守记录数和详情预算。`pagination.reconcile` 记录这类批次变化。

## 本地 Web UI

Web UI 是现有两个 CLI 的可选操作入口，默认监听 `127.0.0.1`，不创建公开分享链接。

```bash
# 仅执行已有规则
uv sync --extra ui

# 同时使用 Agent 生成规则
uv sync --extra ui --extra agent
uv run playwright install chromium

# Agent 使用前，按上文配置 API Key、模型及浏览器环境变量
uv run autoscraper-ui
# 可选：--port 7861 --runtime-dir .agent-runtime/ui

# 开发时通过 Tailscale IP 访问，并在 Python 源码变化后自动重启
uv run autoscraper-ui --host 100.x.y.z --reload
```

表单会自动记在当前浏览器的 `localStorage` 中，刷新或重新打开相同地址后恢复；点击“恢复默认设置”重置表单。“测试任务预设”在页面加载和打开下拉框时读取仓库内的 `examples/ui_tasks.json`，修改文件后重新打开下拉框或刷新页面即可更新，无需重启服务；选择任务时也会读取最新内容。`name` 为显示名称，其他字段使用表单字段名（例如 `url`、`task`、`fields`、`count`），选择后仍可手动修改。

`--host` 可指定开发机的 Tailscale IP，仅供可信设备访问，访问范围由本机网络和 Tailnet ACL 控制。浏览器窗口仍在服务所在的开发机上运行。`--reload` 使用 watchfiles 监控实际 `src/autoscraper/` 下的 Python 源码，变化后完整重启并保留启动参数；当前测试任务会中断，需自行重新运行。日志、JSON 和其他运行产物变化不会触发重启。不加 `--reload` 时仍为普通启动。

打开终端给出的本机地址。在“Agent 生成规则”中输入 URL 和自然语言需求，可展开“已有规则（可选）”上传或粘贴参考 ScraperSpec JSON，并在任务中说明修改意见；默认只生成并试抓取验证，勾选“执行完整抓取”后才要求完整执行。在“执行已有规则”中上传或粘贴 ScraperSpec JSON，即可重复抓取，无需模型依赖或 API Key。高级设置覆盖模型、步骤和等待预算、超时、必需字段、数量及验证上限。可选的每次抓取上限、验证超时、内容等待超时和导航超时填写 0 或留空均表示不覆盖默认值；修复次数和内容等待模型调用预算中的 0 仍表示零次。

页面持续显示 browser-use 的 INFO 日志，包括 Step、Memory、工具调用和参数；这些是模型自报信息，不代表独立验证事实。运行完成后可查看结构化摘要、验证反馈和最多 100 条预览，下载规则及完整 JSON/CSV 数据。`sample_only` 表示样本，`rule.unvalidated.json` 表示失败时保留的未验证候选，不能当作已验证规则。

需要澄清时，网页显示问题、原因、建议选项和自由回答框；选择选项或输入文字后点击“提交回答 / 继续”。自由回答优先于选项。需要登录等人工操作时，须先勾选“显示浏览器”，在独立浏览器中操作，再回网页点击继续；页面也可结束等待或取消接管。回答后继续同一次任务，不重新创建浏览器。等待不占用 Agent 的任务超时，但超过 15 分钟未处理将结束当前调用并返回待处理状态。任务结束后不支持原地恢复，需要重新运行；请保持原网页打开，刷新网页不会恢复原任务的交互控件。

同一 UI 进程只允许运行一个任务，包括等待人工操作的任务；其他标签页不会绕过限制。不要在网页或澄清答案中输入登录密码、验证码或 API Key，登录操作在浏览器中完成。

认证状态使用名称管理，例如 `demo-account`，只接受英文字母、数字、下划线和连字符。同名复用本机登录状态，不接受任意认证文件路径。默认目录如下，可在停止 UI 后自行清理：

- `.agent-runtime/ui/artifacts/<任务 ID>/`：规则和数据下载文件。
- `.agent-runtime/ui/auth/<名称>.json`：私有认证快照，禁止网页下载。
- `.agent-runtime/ui/traces/`：原有 Trace 记录，禁止网页下载。

安装 UI 不影响 `autoscraper` 和 `autoscraper-agent` 的使用；CLI 的路径和交互行为保持不变。

## 浏览器网络 JSON 数据源

当字段只存在于 Fetch/XHR 的 JSON 响应中时，Agent 可以生成 v3 网络规则。
采集在首次导航前安装，`list_network_responses` 列出候选元数据，
`inspect_network_response` 查看有界样本，`test_network` 测试原样规则及去重键。
旧 v1/v2 DOM 规则继续使用原执行路径，CLI/UI 的操作方式不变。

可先启动不依赖外网的示例页面：

```bash
uv run python examples/network_demo.py
```

在另一个终端执行已有网络规则（不调用模型）：

```bash
uv run autoscraper examples/books.network.json --output output/network-books.json --output output/network-books.csv
```

也可以让 Agent 从页面发现这些字段并生成规则：

```bash
uv run --extra agent autoscraper-agent "http://127.0.0.1:8765/" "抓取前10本书，字段为id、title、stock、price、active，包括页面未展示的库存、价格和启用状态，点击Next抓取后续数据" --fields id title stock price active --count 10 --spec-output output/network-rule.json --output output/network-data.json
```

页面只显示书名，接口额外包含库存、小数价格及布尔值；示例共两条，按钮禁用后正常结束。
规则通过浏览器导航和点击触发请求，不拼装或重放 API，不下载资源文件。
支持初始加载、Next 链接、点击和无限滚动分页；不支持 DOM/网络字段混合或网络详情关联。
网络协议、缓存限制和分页完成语义见 [协议参考](docs/reference.md#浏览器网络-json-v3)。


同一个示例服务还提供 `http://127.0.0.1:8765/scroll`。将示例规则的 `start_url` 改为此地址，
并将 `network.pagination` 改为以下配置，即可验证 DOM 节点数不变的网络滚动加载：

```json
{"type":"scroll","loading_selector":"#loading","end_selector":"#end","max_scrolls":20,"no_change_limit":3}
```

Agent 使用原有 `explore_scroll(network=最终网络规则, dedupe_by=最终去重键)`，不需要 DOM
记录选择器。页面和容器滚动复用现有动作；`container_selector` 指定唯一容器。
网络滚动只有成功提取并去重后的新增记录才算推进。`max_pages` 统计初始批次与新增数据批次，
空滚动/重复批次只消耗 `max_scrolls`。`end_marker` 表示明确结束；没有结束标志时，
到底、无加载且无在途响应后连续多轮无新增，才返回启发式的 `no_more_content`。
已观察到的加载或响应等待超时会失败，`scroll_limit`/`max_pages` 是预算截断。


字段名要求与值完整性分开：用户要求的字段必须在规则中声明，但源数据中的 `[]`、`""`、
`null`、零和 false 都会保留。个别 JSON 键/DOM 元素缺失输出 null，不丢弃整条记录；
整列请求字段从未定位、类型不兼容或错误的记录根仍会反馈修复。
旧规则的 `required` 兼容保留为整次运行的定位证据要求，不再限制每条值非空。
网络工具的 `diagnostics` 给出响应、记录、字段、路径和原因，`field_coverage` 给出定位计数，
无需通过反复删除字段猜测失败原因。详见协议参考中的字段语义说明。
