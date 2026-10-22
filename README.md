# AutoScraper

输入网站 URL 和自然语言需求，由 browser-use Agent 探索网页并生成抓取规则，再交给 Playwright Executor 导出数据。

## 运行 Agent

以下命令使用 **Windows PowerShell**，需要 Python 3.12 和 uv。

### 1. 安装依赖和浏览器（首次运行）

```powershell
cd C:\MySpace\program\AutoScraper
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

```powershell
$env:DEEPSEEK_API_KEY = "替换为你的 DeepSeek API Key"
$env:AUTOSCRAPER_BROWSER_PATH = uv run --extra agent python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
```

如果终端已经配置 `DEEPSEEK_API_KEY`，无需再次赋值。默认接口为 `https://api.deepseek.com/v1`，模型为 `deepseek-flash`。

### 3. 启动 Agent，生成规则并抓取数据

```powershell
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书，字段名为title、price、url，分别为完整书名、价格、绝对详情链接，需要分页" --spec-output output/books-agent.json --output output/books.json --output output/books.csv --headed
```

`--headed` 显示 Agent 探索浏览器；去掉后使用无头模式。此命令会调用 LLM，产生 API 费用。

成功后得到：

| 文件 | 内容 |
| --- | --- |
| `output/books-agent.json` | Agent 生成的可复用抓取规则 |
| `output/books.json` | 抓取数据，JSON 格式 |
| `output/books.csv` | 抓取数据，CSV 格式 |

换网站时，替换命令里的 URL 和自然语言需求即可。

## 只生成规则 / 重复抓取

**只探索并生成规则**：不传 `--output`。

```powershell
uv run --extra agent autoscraper-agent "https://books.toscrape.com/" "抓取前25本书的完整书名、价格和详情链接，需要分页" --spec-output output/books-agent.json
```

**用已有规则抓取**：运行 Executor，不调用 LLM。

```powershell
uv run autoscraper output/books-agent.json --output output/books.json --output output/books.csv
```

也可直接使用仓库中已验证的 Agent 规则快照：

```powershell
uv run autoscraper examples/books.agent-generated.json --output output/books.json
```

## 常用配置

| 参数 | 用途 | 默认值 |
| --- | --- | --- |
| `--spec-output` | 规则输出路径，必填 | — |
| `--output` | 数据输出路径，可重复指定 CSV / JSON | 不抓取数据 |
| `--headed` | 显示探索浏览器 | 无头模式 |
| `--model` | LLM 模型名 | `deepseek-flash` |
| `--base-url` | OpenAI-compatible 接口地址 | `https://api.deepseek.com/v1` |
| `--max-steps` | 最大探索步数及 LLM 调用数 | 12 |
| `--timeout` | 探索总超时，秒 | 240 |
| `--llm-timeout` | 单次 LLM 超时，秒 | 45 |
| `--max-output-tokens` | 单次 LLM 输出 token 上限 | 3000 |
| `--browser-path` | Chromium / Chrome 可执行文件路径 | 读取环境配置或自动查找 |
| `--structured-output` | 开启服务端 JSON Schema 模式 | 关闭，以兼容更多接口 |

其他兼容接口可设置 `AUTOSCRAPER_API_KEY`、`AUTOSCRAPER_BASE_URL` 和 `AUTOSCRAPER_MODEL`。密钥优先读取 `AUTOSCRAPER_API_KEY`，其次读取 `DEEPSEEK_API_KEY`；配置不自动加载 `.env`。

完整参数：`uv run --extra agent autoscraper-agent --help`。

## 浏览器与缓存路径

如果之前使用了项目内的 `.browsers`，在上面的安装、浏览器路径获取和运行命令**之前**执行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.browsers"
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
```

这两个变量只在当前终端有效，新终端需重新设置。也可直接用 `--browser-path` 指定本机 Chrome 路径。

## 测试

默认测试不调用真实 LLM：

```powershell
uv run --extra agent pytest
```

付费 LLM 测试单独启用，需要前面的密钥和浏览器配置：

```powershell
$env:RUN_LLM = "1"
uv run --extra agent pytest -m llm
```

原 Executor 的线上测试用 `RUN_LIVE=1` 和 `pytest -m live` 单独启用。受限 Windows 环境可以追加 `--basetemp .pytest-tmp-local`。

## 项目结构与当前范围

```text
自然语言需求 → browser-use Agent → ScraperSpec JSON → Executor → CSV / JSON
```

Agent 负责探索和生成规则，Executor 只负责执行。当前支持列表字段、文本/属性提取、绝对 URL、Next 链接分页、数量限制和去重，不接受模型生成的 Python/JavaScript 脚本。

真实验证：DeepSeek 在未提供 CSS Selector 的情况下，7 步生成规则并抓取 25 本书；同一规则用原 CLI 重跑结果一致。回归测试 42 项通过，2 项线上测试默认跳过。

模块边界、协议语义、工具限制和验证记录见 [协议与实现参考](docs/reference.md)。
