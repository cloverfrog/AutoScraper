# 轻量级 Benchmark

本工具用于后续简历项目的可审计量化评测：不同网页结构的任务成功率、Feedback Repair
Loop 的提升与成本，以及生成 ScraperSpec 后重复执行的成本。不改变 AutoScraper 核心行为，
不引入数据库、UI、调度框架或额外的模型裁判。

现阶段只交付基础设施与无模型验证。六个任务是便利样本，不能代表所有网站；固定模型名
也不能保证供应商后端不变。正式付费实验及原生 browser-use 对照留到下一阶段。

## 复用的现有能力

- Agent 调用现有 `autoscraper-agent` CLI，保留其非交互、输出及退出码契约。
- 已有规则调用现有 `autoscraper` CLI，外层使用已有 `Trace/FileSink`，不增加追踪系统。
- 请求、Token、阶段耗时和修复次数来自 `summary.json` / `events.jsonl`。
- 独立评分不调用工作流验证器，不信任 `task_fulfilled`，不导入 AutoScraper、Playwright
  或 browser-use，不需要联网。

工作流的 `count` 是可复用上限，自然结束不足上限也可能成功；Benchmark 要求冻结任务的
确切记录集和数量。工作流允许部分真实空值，并不验证任意业务语义；Benchmark 对每个
目标字段独立比对。这两种成功口径不可混用。

## 任务及 Ground Truth

任务原文、字段类型、数量、身份键见 `benchmarks/tasks.json`：

| ID | 页面结构 | 数量 | 字段 |
|---|---|---:|---|
| books-list | Books to Scrape，跨链接分页 | 25 | title, price, url |
| books-detail | Books to Scrape，一层详情 | 5 | title, price, url, description |
| quotes-delayed | Quotes to Scrape，延迟渲染与分页 | 25 | quote, author, tags |
| cars-load-more | Web Scraper 车辆测试页，点击追加 | 25 | name, description, year, status, url |
| quotes-scroll | Quotes to Scrape，滚动追加 | 25 | quote, author, tags |
| local-network | 仓库 network_demo，点击获取 JSON | 2 | id, title, stock, price, active |

车辆任务保留页面实际显示的 `Year: 1955` 和 `Available: 1` 等整段文本。
最初方案要求剥离标签与库存数字，但现有 FieldSpec 无通用文本变换能力。任务因此调整为
当前协议可表达的提取要求，不添加站点特判，不修改核心协议。价格保留 `£`；书名取完整
标题而非列表截断文字；详情描述保留站点自身重复、`...more` 等内容，不做“纠错”。

`benchmarks/truth/` 中的 JSON 是冻结答案，`benchmarks/evidence/` 保存对应渲染 HTML 或
源 JSON、来源 URL 和 SHA-256。采集器 `benchmarks.oracle` 只依赖公开页面 DOM/JSON，
不调用 AutoScraper 的 Executor、规则或输出；站点选择器只存在于独立核验器和明确标记的
示例 Spec 内，不提供给 Agent。local-network 答案还与示例定义中的两个字面记录核对，
包含负数、零及布尔 false。

本版元数据真实标记为 `source-reviewed`：Codex 已检查源字段、顺序、分页边界、完整
文本和本地字面值，**尚未声明由真人完成审阅**。正式付费研究前应由维护者检查原始证据，
在新的 Commit 中记录人工审阅者与结论；本阶段冻结答案用于基础设施验证。

采集候选答案必须使用独占的新目录，例如：

```bash
uv run python -m benchmarks.oracle --task books-list --capture /tmp/books-truth-candidate
```

该命令不更新正式答案。审阅后如确有页面版本变化，应显式形成新的任务/答案版本与 Commit，
不能根据被测 Agent 的输出修正答案，也不能将不同答案版本的成绩混在一起。

### 比较规则和成功标准

- 文本只做 Unicode NFC、首尾清理和空白折叠；不做大小写、标点、货币或语义模糊匹配。
- URL 必须是 HTTP(S) 绝对链接，仅归一化协议/主机大小写与默认端口，保留路径、查询、
  片段和尾斜线。相对 URL、携带凭据的 URL 不合格。
- tags 必须为字符串列表，顺序与页面一致；字符串拼接不等同于列表。
- 数值按 Decimal 比较，允许数值 0 与 0.0 等价，拒绝数值字符串、NaN/Infinity；
  false 与 0 严格区分。当前六任务没有 nullable 字段，缺字段和 null 都不合格。
- 书籍、车辆以 URL 为身份键，语录以完整 quote 为身份键，本地示例以数值 id 为键。
  每个身份只匹配一次，采用首次出现项，不从重复记录中挑选较好的值。
- 额外非目标字段不参与比较；额外、缺失、重复记录均扣分。

严格任务成功要求进程退出码为 0、未超时、CLI 确认完整执行且无提取错误、数据文件有效，
并且记录数量、身份集合、顺序、所有目标字段全部正确。单独有正确数据或自报 success
均不充分。无输出、异常、unsupported、待澄清、待人工操作均不能算成功，不自动回答或重试。

设标准记录数为 G，实际输出数为 P，完整正确匹配数为 C：

- 记录准确率 `C / max(G, P)`；记录 Precision `C/P`（P=0 时为0），Recall `C/G`。
- 每字段准确率 `该字段正确匹配数 / max(G, P)`，缺失/错误身份对应字段均记错。
- 这些准确率衡量最终交付数据；工作流未发布数据时记为0，不反推内部试抓取的字段质量。
- 顺序错误仍可得到较高字段/记录准确率，但严格任务成功为 false。
- 成功率按组及逐任务报告成功数/已启动数；完整批次还输出按任务和重复编号配对的
  Repair 成功率差（百分点）及请求、Token、耗时、修复次数的平均增量。

## 使用方法

从仓库根目录执行。先按 README 安装锁定依赖及 Chromium；无模型组只需要项目基础依赖。
开发测试需要 dev 依赖；下一阶段 Agent 运行需要 `--extra agent` 对应依赖和模型凭据。

### 1. 无模型评分测试

```bash
uv run pytest tests/benchmark -q
uv run python -m benchmarks --help
```

测试不启动付费模型。现有 tracing 回归可单独运行：

```bash
uv run --extra agent pytest tests/observability/test_tracing.py -q
```

### 2. 仅检查页面可执行性与答案漂移

```bash
uv run python -m benchmarks check
uv run python -m benchmarks check --tasks books-detail local-network
```

check 不运行 Agent 或 Executor。它独立采集当前源数据并严格比对冻结答案，同时检查本地
证据哈希。结果、原始证据和错误保存在 `output/benchmark-checks/<唯一批次>/`。
HTML 中无关内容的变化不直接构成漂移，比较的是独立采集出的目标数据。

每次 run 都自动预检所选任务，不提供跳过开关。任务漂移或不可访问时，其所有实验组均
标记 blocked，不根据某组输出选择性排除。源故障不是模型失败；启动后的网络/浏览器故障
计入该次失败并保留原因。正式结论应检查完整批次、可用任务集和源检查证据。

### 3. 两任务无模型验证

```bash
uv run python -m benchmarks run --commit "$(git rev-parse HEAD)"
```

默认 `benchmarks/configs/smoke.json` 只运行 books-detail 与 local-network，各一次，使用
基于仓库示例裁剪记录上限的已知 Spec，零 LLM 请求。它不证明规则生成成功率。

正式运行要求完整40位 SHA 与当前 HEAD 相等，工作区无已跟踪或未跟踪代码改动；工具不会
自动检出、拉取或重置分支。尚未提交的本地开发验证可显式加 `--allow-dirty`，结果会标记
`formal: false`，不能冒充固定代码版本的正式实验。

配置也可提供 `commit`；若命令行与配置同时指定，必须一致。相对 Spec 路径相对于仓库根。
可用 `--config`、`--output-root` 指定其他配置和产物根目录。所有金额信息均不作为硬预算；
预算由现有请求上限、Step 上限、每请求输出 Token 上限及时间限制控制。

### 4. 下一阶段 Repair 实验（本阶段不执行）

```bash
# 会产生模型费用：先完成人工源答案审阅，检查配置、模型和凭据，再显式启用。
uv run --extra agent python -m benchmarks run \
  --config benchmarks/configs/repair.json \
  --commit "$(git rev-parse HEAD)" --allow-llm
```

只要配置包含 Agent 组而未提供 `--allow-llm`，整个批次会在任何源预检或模型调用前拒绝。
配置不接受任意 shell 命令。API Key 仍由生产 CLI 从环境读取，不写入配置、产物或完整
环境快照；捕获的进程输出复用现有 Sanitizer 清理明确凭据。

配置字段为 tasks、groups、repeats、commit、model、base_url、budget、process_timeout、
browser_path；组字段为 id、mode（agent/executor）、protocol、budget、specs、reuse_runs。
组预算覆盖公共预算；未知键、无效范围及修复关闭但预留非零的配置均拒绝。
所有任务使用明确的 fields/count 参数；需求解析仍由现有流程执行并计费，不通过测试替身
跳过。validation 的范围保持所有组一致，默认覆盖本版任务的完整记录数。

### 5. 复用与离线重评分

在组配置中加入 `"reuse_runs": 4` 即得到总计5次抓取：首次运行加4次独立重放。
executor 组必须为每个任务明确提供 `specs` 映射；agent 组不得提供预填规则。
同一生成运行的规则只供其自身后续重放，新进程/浏览器不使用探索缓存。
生成失败或独立评分失败时，后续重放标记 blocked，不回退到已知好规则。
本地示例通过 benchmark 包装绑定临时端口，原 Spec 副本、哈希及绑定后的副本全部保留。

```bash
uv run python -m benchmarks score output/benchmark/<批次目录>
```

score 使用该运行保存的 task/truth 副本并验证哈希，不读取最新标准答案、不访问网络。
可重复生成 score.json 与 summary.json/csv/md，不覆盖规则、原始数据或追踪。
输出区分计划、已启动、pending、blocked；失败计入已启动分母，不完整批次明确标记。
生成与重放成绩分别汇总，不能把重放成功计入生成成功率。

未来 browser-use 原始记录也可直接调用纯评分接口，无需模拟 AutoScraper 状态：

```python
from benchmarks.common import HOME, read_json, tasks
from benchmarks.scoring import score_records

task = tasks()["books-list"]
truth = read_json(HOME / "truth/books-list.json")["records"]
raw_records = read_json("native-browser-use-records.json")
result = score_records(task, truth, raw_records, execution_ok=True)
```

这里的 execution_ok 必须由对照适配器根据真实退出码、超时和完整性设置。下一阶段适配器
应保留原始输出及可审计的 JSON 解析过程，不用另一轮 LLM 修补。统一批次导入采用同样的
task.json、truth.json、request.json（输入哈希及 mode）、records.json、process.json
（exit_code/timed_out/wall_seconds）、execution.json（complete）和 manifest 单元结构；
不提供计量追踪时 Agent 成本为未知，不记零。本版没有可执行的 browser-use 对照组。

## 成本口径与公平性

### Repair 预算

| 协议 | 组 | 主模型硬上限 | 修复预留 | 最大修复 | 首轮请求上限 |
|---|---|---:|---:|---:|---:|
| equal-total | total-no-repair | 24 | 0 | 0 | 24 |
| equal-total | total-repair | 24 | 0 | 2 | 24 |
| equal-initial | initial-no-repair | 18 | 0 | 0 | 18 |
| equal-initial | initial-repair | 24 | 6 | 2 | 18 |

现有默认开启 Repair 时首轮最多18请求，关闭时自动取消预留而可用24请求；直接比较默认
开关会混入首轮预算差异。本版同时保留上表两套对照，不混合成功率。
第一套回答相同总资源上限内的策略收益，但可能没有余量修复；第二套回答相同首轮额度
下额外修复预算带来的收益，并明确总预算不同。

其他配置统一为每轮12 Step、等待模型最多8请求、工作流240秒、每请求输出最多3000 Token，
外层完整子进程600秒，全部6任务各重复3次，共72个初始任务运行。每个重复轮次交替正反
组序，减少总是先运行某组的时间偏差；不声称消除全部站点或供应商时间效应。

主请求包括需求解析、探索、修复及结构化失败重试，不等于 Agent Step。
语义等待模型的最多8请求独立于主预算，因此还需报告两者合计（24主请求组最多32请求）。
两套协议均保持相同等待上限，不把等待成本隐藏在主请求之外。
剩余主请求不足3次时修复不会启动；只有可恢复反馈才会开放修复余量。
当前会话内提交失败后的继续修正不算工作流 Repair；次数以 `repair.started` 为准。
未启动原因保存在工作流结果/追踪中。

相同首轮预算不保证相同初始候选。重复编号只用于匹配任务配置、计算观测差值，不能解释
为共享随机种子或严格的同候选因果实验。后续如需受控故障注入，应另立实验，不混入自然
任务成功率。

### 指标的完整性

每次运行报告 requirements/main/wait 请求数及其合计、prompt/completion/total/cached
Token、usage_calls、usage_missing_calls、阶段耗时、墙钟时间和修复次数。
Token 没有 Usage 时为 null；部分可得时只汇总已报告值，同时报告缺失覆盖率，不估算费用。
配对 Token 增量只使用两侧完整追踪、无缺失 Usage 且该 Token 字段均存在的运行。
无模型 executor 请求与 Token 为确定的零，不能将这种规则重放成本称为规则生成成本。

summary 损坏或强杀只剩事件时，指标标记为下界；无任何事件的 Agent 请求/Token 为未知。
trace_errors 或异常终止会标记计量不完整。未知值不按零补齐，成本增量保留实际可比较
配对数。wall_seconds 包含子进程启动、浏览器版本探测、执行、导出及关闭，不包含批次
源预检；trace duration 更接近工作流边界。嵌套阶段时长不能直接求和。

### Spec 复用与下一阶段 native baseline

令 N 为需要交付的总抓取次数，首次生成包含第一次数据交付，则：

`AutoScraper总成本(N) = 生成并验证执行成本 + (N-1) × 后续执行成本`

`native总成本(N) = N × 每次直接Agent抓取成本`

对 N=1、5、10 分别比较请求、Token、时间、严格成功率和累计成本。实际执行应使用各次
观测成本求和，而非假定每次完全相同。不得拿手写规则的零生成成本代替真实生成成本；
生成失败的后续 blocked 也不是零成本成功。生成/重放失效率和完整交付数需同时报告。

原生 browser-use 对照需固定相同底层版本、供应商模型、字段/顺序/数量、浏览器环境、
请求及时间预算和独立评分；允许使用原生工具但不注入 AutoScraper Spec、标准答案或
站点选择器。记录真实请求重试与 Token，不能以 Step 充当请求。正式实现和运行均不在本阶段。

## 产物与可复现性

```text
output/benchmark/<时间_UUID>/
  manifest.json                  # 完整展开计划、配置、Git/依赖信息和状态
  checks/<task>/                 # 各组共享的独立源核验与新证据
  runs/<task_group_repeat_UUID>/
    request.json task.json truth.json
    spec.json [bound-spec.json] records.json
    browser.json process.json execution.json
    trace/<原有run目录>/...
    score.json
  summary.json summary.csv summary.md
```

每次运行保存 Spec 来源与 SHA、任务/答案 SHA、Git SHA、dirty 标记、锁文件 SHA、Python、
项目和浏览器相关包版本，以及浏览器版本/可执行路径。输入和输出目录独立，不覆盖旧运行。
单次失败不重置预算，不自动补跑。子进程超时终止整个 POSIX 进程组，保留已刷新事件；
Windows 仅保证终止 Python 子进程，本版实际验收环境为 Linux。

公开页面会变化，重跑不能只凭 Git SHA 保证同一数据；必须同时检查任务、答案和源核验。
供应商未公开 seed 时不伪造确定性保证。没有大样本与 native 实验前，不生成“提升X%”等
简历结论，也不把基础设施的两任务全对解读为 AutoScraper 整体成功率100%。

## 本次验收

无模型测试覆盖评分、类型、错误详情、假成功、损坏/中断日志、未知 Usage、独立目录、
重复、复用、源漂移、固定 Commit、预算展开、进程超时、凭据清理与付费开关。
新的浏览器端到端验证仅运行 books-detail 和 local-network 两个任务；其他页面只做源
可执行性核验。具体验收数值与路径由最终交付说明给出，原始记录位于本机 output 目录。

2026-11-02 开发验证：Benchmark 测试55项、既有 tracing 测试46项，共101项通过；
Ruff 检查和格式检查通过。两个任务的记录及所有目标字段准确率均为1，且无提取错误：

| 任务 | 记录数 | LLM 请求 / Token / Repair | 外层墙钟时间 |
|---|---:|---|---:|
| books-detail | 5 | 0 / 0 / 0 | 6.47 秒 |
| local-network | 2 | 0 / 0 / 0 | 1.71 秒 |

产物批次为 `output/benchmark/2026-11-02T09-46-27.823508+00-00_a3b29c9cbf`，
使用 `--allow-dirty`，因此明确属于非正式的基础设施验证。
六个源任务均与冻结数据一致：一次全量源检查中滚动页因加载提示动画导致核验器过早滚动，
原失败记录保留；修正核验器等待顺序后，单独复核通过，未改变答案或生产代码。
