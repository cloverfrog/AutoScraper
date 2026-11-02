# Issue #40：数字分页与混合分页探针

关联 [Issue #40](https://github.com/cloverfrog/AutoScraper/issues/40)。本次只调查网站行为，
不修改生产分页协议、执行器或 Agent 提示词，不代表整个 Issue 已完成。

## 运行

安装项目依赖与 Playwright Chromium 后执行：

```bash
uv run python scripts/probe/probe_pagination.py --verify-executor
```

默认分别访问 HN Algolia 和 iNaturalist。只调查一个网站：

```bash
uv run python scripts/probe/probe_pagination.py --site hn --verify-executor
uv run python scripts/probe/probe_pagination.py --site inaturalist --headed
```

可用 `--browser-path` 或 `AUTOSCRAPER_BROWSER_PATH` 指定 Chromium。
`--headed` 需要可用图形环境，仍使用临时、无登录状态的浏览器上下文。
脚本不调用模型，不读取账号或 API Key，不处理验证挑战。

输出默认位于 `output/pagination-probe/<UTC时间>/`。`--output` 可指定尚不存在的目录，
防止不同实验日志混在一起。每个网站保存 JSONL 逐步日志、截图，运行结束保存
`report.json`。每个动作记录动作前后状态、活动页码、全部分页区域、记录身份链接、
新增身份、与上一批重叠数、滚动位置和等待时间。原始 HTML、请求头和 Cookie 不保存。

默认动作预算为 HN 连续点击 8 次，另做一次末页、一次首页对照；iNaturalist 滚动 8 次。
`--hn-steps` 和 `--inat-scrolls` 可设为 1～15，`--wait-ms` 可设为 1000～15000，默认 4000。
每次动作等待完整观察窗口，不以活动页码先变就提前判定列表加载成功。窗口结束后的迟到
响应仍可能遗漏，因此这是有界观察，并非生产加载完成判定。

`--verify-executor` 会额外在独立上下文使用现有 `ScraperExecutor` 抓取 HN，
提取标题和稳定 HN 详情链接，以链接去重；此过程不继承探针已打开的列表状态。
`report.json` 保留完整规则、记录身份和执行结果。

退出码 0 表示所选网站都取得了可观察列表；不等于所有分页验收通过。
退出码 1 表示存在访问阻塞、无列表或执行错误，仍应检查报告与已保存的 JSONL。

## 判断方法

- 相邻推进：前后只有一个一致的活动页码，且后者恰好加一。多个区域显示相同活动页码
  可以合并；若不同区域活动页码冲突，报告 `adjacent_page: null`，不猜测正确页码。
- 新增记录：用稳定身份链接与当前实验阶段的累计集合比较。内容变了、顺序变了或
  DOM 数量增长，都不能独立证明有新增记录。
- 双箭头对照：先重开第一页，再点击右双箭头，之后点击左双箭头，记录实际页码。
  日志中的累计去重集合仍保留前面的连续遍历；`sequential_unique_records` 仅统计连续阶段。
- 多控件：完整记录各区域的可见性、文字、链接和禁用状态；点击选择器必须恰好匹配一个
  可见且可用的控件。探针不会静默选择第一个按钮。
- 滚动：使用现有执行器的真实滚动动作，每次一个视口；没有新增不自动宣告结束，
  不自动点击 Next。加载元素信息只是通用观察线索，不能视为网站专用的可靠加载标志。

iNaturalist 若可访问，先保存分页区域和候选身份链接，再观察滚动。
其身份目前按 `/observations/<数字>` 链接收集，属于待现场确认的探针假设：
`record_nodes` 为匹配链接节点数，可能包含一条记录的多个链接，`ids` 才是唯一身份集合，
不能据此前者断言列表卡片数量。此阶段也不验证物种、日期、地点、观察者四个字段。

若已从实际 DOM 找到唯一 Next 选择器，可显式传入：

```bash
uv run python scripts/probe/probe_pagination.py --site inaturalist \
  --inat-next-selector '<已观察并确认的唯一 CSS>'
```

此时先在初始列表测试独立点击，再重新加载初始列表、重置身份集合，观察滚动，
最后测试滚动后点击。需要人工比较各阶段的页码、身份与目标链接；探针不会据此自动
生成通用的滚动转点击规则。若匹配多个元素，保存歧义结果并拒绝点击。

## 本机结果（2026-11-02）

最终实测结果和稳定身份保存在 [实验记录](probes/pagination-2026-11-02.json)。
原始日志及截图位于 `output/pagination-probe/verified-20261102/`，未纳入 Git。

HN Algolia：

- 初始每页 30 条。下述同一选择器连续推进 1 → 2 → … → 9，8 次点击均恰好匹配一个
  控件，活动页码每次加一，每次新增 30 个身份，累计 270 个唯一身份。
- 到第 7 页时，数字窗口左边界从 1 移到 2；第 9 页窗口为 4～14。
  动态相邻选择器仍有效，不必根据页码重写规则。
- `.Pagination_next button` 从第 1 页直接到第 34 页，该页 10 条记录；
  `.Pagination_previous button` 再回第 1 页，确认双箭头分别是末页和首页。

```css
.Pagination_item-current + .Pagination_item:not(.Pagination_next) button
```

现有声明式协议可使用 `type: click`、`mode: replace` 加上述 CSS。
独立执行器实测同样抓取 9 页、270 条唯一记录，重复数为 0，无错误；
停止原因为 `max_items`，最终活动页为 9。完整规则见实验记录的 `hn.executor.spec`。

这说明 HN 数字分页可以先复用现有协议。仍需改进 Agent 对控件语义和相邻推进的验证；
一次记录变化不能区分“相邻页”和“末页跳转”。此次没有遍历全部 34 页，也没有验证
自然耗尽、虚拟列表或列表持续更新时的完整性。

iNaturalist：

- 默认无登录 Chromium 上返回 HTTP 403，页面内容为 Cloudflare 的
  `Performing security verification`，未出现观察记录与分页区域。
- 记录为 `blocked_or_no_records`；没有执行滚动或点击，不能确认三个分页区域，
  也不能确认滚动与数字分页的衔接时机。
- 后续需要在可正常访问该列表的环境重新运行探针，再确定是否需要混合协议。
  本次结果不足以支持“滚动没新增就点击下一页”等切换策略。

## 离线检查

```bash
uv run pytest -q tests/scraping/test_pagination_probe.py
uv run ruff check scripts/probe/probe_pagination.py tests/scraping/test_pagination_probe.py
```

模拟浏览器页面覆盖数字窗口移动、双箭头跳页和末页无相邻按钮，另检查多按钮不执行、
重复批次无新增以及多个区域活动页码冲突。它们检查探针不会给出错误证据，
不替代 iNaturalist 的真实网站验证。
