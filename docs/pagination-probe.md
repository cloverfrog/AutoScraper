# Issue #40：数字页码分页

本次仅处理 [Issue #40](https://github.com/cloverfrog/AutoScraper/issues/40) 的数字页码部分。
复用现有 `click` + `mode: replace`，不新增分页类型。其他分页组合留待后续处理。

Agent 探索指引增加以下要求：从实际 DOM 中识别当前活动页和下一数字按钮，优先使用
相对当前活动页的动态 CSS；区分首页、末页与相邻页，不固定选择第 2 页，也不因类名
包含 next 或图标是双箭头就认定为下一页。点击后同时观察 N → N+1 和记录字段更新。
通用 `sample_passed` 不代表分页控件唯一，仍需检查匹配总数恰好为 1。
若无法用可靠 CSS 定位相邻数字页码，应报告限制，不跳页或添加任意脚本。

这次修改提升 Agent 的探索指引，并用执行器回归验证现有协议的适用性；没有增加
自动识别所有网站页码语义的运行时机制，也不保证任意数字窗口结构都能支持。

## 实测结果（2026-11-02）

HN Algolia（`https://hn.algolia.com/?q=Python`）：

- 每页 30 条，使用下面同一 CSS 连续推进 1 → 2 → … → 9。8 次点击均匹配唯一控件，
  活动页码每次加一，每次新增 30 个身份，累计 270 个唯一身份。
- 第 7 页数字窗口左边界从 1 移到 2；第 9 页窗口为 4～14。同一 CSS 继续有效。
- `.Pagination_next button` 从第 1 页跳到第 34 页，该页 10 条；
  `.Pagination_previous button` 返回第 1 页，确认双箭头分别表示末页和首页。
- 现有执行器独立使用 `click/replace` 同样抓取 9 页、270 条唯一记录，重复数 0、
  无错误，停止原因为 `max_items`，最终活动页 9。身份集合与连续探针一致。

```css
.Pagination_item-current + .Pagination_item:not(.Pagination_next) button
```

这是根据 HN 实际 DOM 推导的探针选择器，没有写入通用 Agent 提示词。
完整规则见 [实测记录](probes/pagination-2026-11-02.json) 的 `hn.executor.spec`。
原始报告、JSONL 和截图保存在本地 `output/pagination-probe/verified-20261102/`，
纳入 Git 的记录仅保留数字页码部分。

## 复现

安装项目依赖与 Playwright Chromium 后执行：

```bash
uv run python scripts/probe/probe_pagination.py --verify-executor
```

可用 `--browser-path` 或 `AUTOSCRAPER_BROWSER_PATH` 指定 Chromium，
`--headed` 打开有头窗口（需要图形环境）。脚本只访问公开列表，不调用模型。
默认连续点击 8 次，另做末页和首页对照；`--hn-steps` 可设为 1～15。
`--wait-ms` 为动作后的完整观察窗口，默认 4000，可设为 1000～15000。

输出默认位于 `output/pagination-probe/<UTC时间>/`，或用 `--output` 指定尚不存在的目录。
保存 JSONL 动作日志、关键截图及 `report.json`；包含活动页码、全部分页控件、稳定
身份链接、新增和重叠、动作前后状态。不同记录不单独作为相邻推进证据，无新增不推断
自然结束。固定等待窗口仍可能遗漏迟到响应，不是生产加载完成判定。
退出码 0 表示取得可观察列表，不代表所有验收通过；必须检查各动作的顺序及新增证据。

## 验证

```bash
uv run pytest -q tests/scraping/test_pagination_probe.py tests/scraping/test_dynamic_pagination.py tests/agent/test_agent.py
uv run ruff check src/autoscraper/agent/explorer.py scripts/probe/probe_pagination.py tests/scraping/test_pagination_probe.py
```

离线浏览器回归覆盖数字窗口移动、双箭头跳末页、多控件拒绝、重复批次、活动页码冲突。
新增生产执行器回归完整遍历模拟网站的 1～12 页，校验访问和提取顺序，跨越窗口后
不跳页，末页不存在相邻按钮时以 `no_next` 自然结束。实站未遍历全部 34 页，
没有运行真实 LLM 端到端测试，也不声称完整解决 Issue #40。
