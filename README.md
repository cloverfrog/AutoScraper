# AutoScraper：第一阶段

Python 3.12 + Pydantic v2 + Playwright + uv。自然语言探索与 browser-use Agent 留给后续阶段；当前实现可独立运行的 JSON 规则协议及确定性执行器。

## 安装与运行

```powershell
uv sync --locked
uv run playwright install chromium
uv run autoscraper examples/books.json --output output/books.json --output output/books.csv
uv run pytest
```

如需把缓存和浏览器保存在项目内（本项目验证使用此配置）：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.browsers"
uv sync --locked
uv run playwright install chromium
```

线上验证显式启用，不影响默认离线测试：

```powershell
$env:RUN_LIVE = "1"
uv run pytest -m live
```

## 模块边界

```text
未来 browser-use Agent → ScraperSpec JSON → ScraperExecutor → records → CSV / JSON
                         spec.py            executor.py               export.py
```

- `spec.py` 仅依赖 Pydantic；通过 `ScraperSpec.model_validate_json(...)` 验证规则，通过 `ScraperSpec.model_json_schema()` 提供未来 Agent 使用的 JSON Schema。
- `executor.py` 接受已验证的规则，管理浏览器生命周期；`run_page` 可注入调用方管理的 Playwright Page，方便离线测试和后续集成。
- `export.py` 处理 UTF-8 CSV/JSON，空 CSV 也保留字段表头；`cli.py` 负责文件读取、调用与退出状态。
- `examples/books.json` 是手写规则；`tests/test_scraper.py` 用 Playwright 路由提供离线 HTML，并提供可选线上测试。

## 规则语义

参考完整示例 `examples/books.json`：`item_selector` 匹配每条列表记录；`fields` 中每个 CSS Selector 相对于该记录解析，必须至多匹配一个元素。`extract` 支持 `text` 或 `attribute`，后者必须指定 `attribute`。文本和属性值会去除首尾并合并连续空白；价格保持字符串，不隐式转换数值。

`transform: "absolute_url"` 使用当前页面最终 URL 解析相对链接，只接受 HTTP(S)。`required` 默认 true；可选字段缺失输出 null。未知协议字段、未知版本、无效数量及无效去重字段会在执行前被拒绝。CSS 语法在浏览器执行时验证。

`pagination.next_selector` 是页面范围的 Next 链接选择器。第一阶段要求唯一匹配、具有 href，按链接导航；不支持依赖 JavaScript 点击的分页或无限滚动。缺少/禁用 Next 正常结束，重复页面 URL 终止循环，`max_pages` 始终限制总访问页数。列表未出现按超时失败，以免规则失效时静默输出空数据。

`max_items` 限制成功且去重后的记录数，达到上限立即停止。`dedupe_by` 指定必填字段组成去重键，留空则按整条记录去重；保留首次出现的记录。

缺失必填字段、多匹配字段或不支持的字段 URL 按 `on_item_error` 跳过并记录错误，或立即失败。导航错误、HTTP 4xx/5xx、无效选择器、异常分页立即失败，不自动重试。结果包含记录、错误、访问页数、重复条数和停止原因。CLI 成功退出 0；跳过错误并导出部分数据退出 2；致命错误退出 1 且不导出本次部分数据。导出文件存在时会覆盖；多个导出文件不保证事务性。

## 测试范围

默认测试实际启动 Chromium，但通过路由拦截所有请求，无需外网。覆盖分页、相对 URL、去重、数量与页数限制、分页循环、可选/必填字段、异常分页、HTTP 错误、协议校验和导出。线上测试断言前 100 条、5 页、URL 唯一、首本标题和价格格式。

当前不实现 Agent、Web UI、数据库、反爬、详情页跟进或复杂动态页面。不接收规则提供的 Python/JavaScript，也不使用 eval、exec 或 page.evaluate。网页自身脚本仍由正常浏览器运行；执行器不是用于不可信网络的安全沙箱。

## 本次验证（2026-10-17）

Python 3.12.10；依赖版本记录于 `uv.lock`。全套测试（包含线上测试）21 项通过，Ruff 检查与格式检查通过。手写 Books 规则实际访问 5 页，获得 100 条唯一记录，无错误、无重复，停止原因为 `max_items`。`output/books.json` 和 `output/books.csv` 均已回读确认 100 条；首本为 `A Light in the Attic`，第 100 本为 `In the Country We Love: My Family Divided`。输出目录已加入忽略规则，可随时重新生成。

受限 Windows 环境下可用 `pytest --basetemp .pytest-tmp-local` 将测试临时文件放在项目内；浏览器进程与线上访问需要环境允许。

接口参考：[Playwright Locator](https://playwright.dev/python/docs/api/class-locator)、[Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)、测试站点 [Books to Scrape](https://books.toscrape.com/)。
