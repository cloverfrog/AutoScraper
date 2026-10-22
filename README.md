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

`--headed` 显示 Agent 探索浏览器；去掉后使用无头模式。此命令会调用 LLM，产生 API 费用。

规则会先校验必需字段、数量并试跑提取；可恢复的错误默认最多修复 2 次。验证通过后得到：

| 文件 | 内容 |
| --- | --- |
| `output/books-agent.json` | Agent 生成的可复用抓取规则 |
| `output/books.json` | 抓取数据，JSON 格式 |
| `output/books.csv` | 抓取数据，CSV 格式 |

换网站时，替换命令里的 URL 和自然语言需求即可。

若字段仅存在于详情页等超出当前协议的场景，会返回 `status: "unsupported"` 并以退出码 3 结束，不再静默省略字段。其他验证失败返回 `unfulfilled` / 退出码 1，保留已有规则和数据文件。具体反馈见终端最后一条 JSON。

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

也可直接使用仓库中已验证的 Agent 规则快照：

```bash
uv run autoscraper examples/books.agent-generated.json --output output/books.json
```

## 常用配置

| 参数 | 用途 | 默认值 |
| --- | --- | --- |
| `--spec-output` | 规则输出路径，必填 | — |
| `--output` | 数据输出路径，可重复指定 CSV / JSON | 不抓取数据 |
| `--headed` | 显示探索浏览器 | 无头模式 |
| `--model` | LLM 模型名 | `deepseek-flash` |
| `--base-url` | 模型接口地址 | `https://api.deepseek.com` |
| `--max-steps` | 需求解析、探索、修复共享的 LLM 调用上限 | 12 |
| `--timeout` | 探索总超时，秒 | 240 |
| `--llm-timeout` | 单次 LLM 超时，秒 | 45 |
| `--max-output-tokens` | 单次 LLM 输出 token 上限 | 3000 |
| `--browser-path` | Chromium / Chrome 可执行文件路径 | 读取环境配置或自动查找 |
| `--structured-output` | 保留的兼容参数 | Responses 适配器始终使用 JSON Schema |
| `--fields` | 明确必需字段名，例如 `--fields title price` | 从任务独立解析 |
| `--count` | 明确请求记录数 | 从任务独立解析 |
| `--max-repairs` | 验证失败后的最大修复次数，0–3 | 2 |
| `--validation-items` | 试抓取最多记录数 | 100 |
| `--validation-pages` | 试抓取最多页数 | 5 |
| `--validation-timeout-ms` | 试抓取的单次浏览器操作超时，毫秒 | 5000 |

其他兼容 Responses API 的接口可设置 `AUTOSCRAPER_API_KEY`、`AUTOSCRAPER_BASE_URL` 和 `AUTOSCRAPER_MODEL`。密钥优先读取 `AUTOSCRAPER_API_KEY`，其次读取 `DEEPSEEK_API_KEY`；配置不自动加载 `.env`。

完整参数：`uv run --extra agent autoscraper-agent --help`。

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

## 项目结构与当前范围

```text
自然语言需求 → 独立需求约束 → Agent 生成规则 → 验证 / 反馈修复 → Executor → CSV / JSON
```

Agent 负责探索和生成规则，Executor 只负责执行。当前支持列表字段、文本/属性提取、绝对 URL、Next 链接分页、数量限制和去重，不接受模型生成的 Python/JavaScript 脚本。

最新验证：DeepSeek 的 title + price 请求成功抓取 25 条；要求额外的 description 时明确返回 unsupported / 退出码 3，无不完整文件写入。回归测试 71 项通过，2 项线上测试默认跳过。

模块边界、协议语义、工具限制和验证记录见 [协议与实现参考](docs/reference.md)。

字段遗漏防护、结果状态和修复策略见 [Validation + Feedback Repair Loop](docs/validation-loop.md)。
