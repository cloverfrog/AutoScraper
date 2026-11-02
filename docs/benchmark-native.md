# 原生 browser-use Baseline

代码为 `benchmarks/native.py`，配置为 `benchmarks/configs/native-pilot.json`。使用锁定的
browser-use 0.13.11 原始 Agent、默认 Tools 和原生 structured done，直接交付 records，
不生成 ScraperSpec，不调用产品探索、提交检查、需求解析或 Repair。原 Pilot 产物、任务、
冻结答案和原始证据均保持不变。

## 接入与输出

- Worker 增加 native 模式，复用现有 Runner 的子进程隔离、源预检、时间限制、输出目录
  和独立评分；真实调用仍需 `--allow-llm`。
- 只复用 LimitedChatOpenAI / ChatOpenAIResponses 和 Trace/StepObserver。不导入
  explorer、专用 tools、submission 或 workflow runner。
- 输入只有原 URL 和原任务文字。输出 Schema 从任务字段名/类型构建，不含真值或选择器，
  不强制 list 长度为25。数量、顺序、字段语义由独立 Scorer 判断。
- 原生 evaluate、extract、find_elements、文件读写等工具均保留。除 DeepSeek 不支持的
  视觉输入外，不删减工具；原生规划、思考字段、失败恢复、评判和压缩保留默认值。
- Agent、extract、judge 和压缩共用同一个受限模型。结构化失败重试按实际请求计数，
  SDK 自动重试为0；辅助调用没有额外免费模型或未计量的接口。
- 原生完成结果原样存为 native-final.txt。宿主只验证 JSON 并解包 records，不删重、
  补字段、改值或排序，不额外调用 LLM 修复格式。无效数据保留原文并判失败。
- 原生内部文件位于自身 FileSystem，available_file_paths 为空，不向 Agent 提供真值、
  预检、历史 Pilot 或参考 Spec 的文件清单。这不是操作系统级敌对代码沙箱。

每次另存 native-settings.json（实际工具/默认选项）、native-browser.json（真实会话 CDP
版本）、native-status.json（done/成功/评判/历史错误）、native-final.txt，以及已有的
request/task/truth、records、Trace、process、execution、score。异常也完整收尾计量，
有正确数据但失败退出不算任务成功。原生自评/judge 只作诊断，独立真值才决定正确性。

## 公平性

| 项目 | 历史 AutoScraper Pilot | 原生 Baseline |
|---|---|---|
| 版本/浏览器 | browser-use 0.13.11，新会话、无头、同 executable_path 选择、关闭默认扩展 | 相同启动参数；额外记录真实会话 CDP 版本 |
| 模型接口 | deepseek-flash / DeepSeek Responses | 相同适配器、模型和接口 |
| 请求参数 | temperature=0、reasoning_effort=none、输出上限3000、单次45秒、SDK重试0、结构化重试2 | 完全一致 |
| 活动/外层超时 | 240秒 / 600秒，另有浏览器收尾 | 相同；工具内部默认超时仍受任务总时限约束 |
| 请求额度 | 主模型24、语义等待另8，理论总上限32 | 全部功能共用32次，无额外请求池 |
| Agent Step | 每轮12；On可新增修复轮次，旧 Pilot 未触发 | 一次 run 最多12，保留原生预警/最后一步 done |
| 输出方式 | 动作 JSON Schema + ScraperSpec，Executor 导出数据 | 动作 JSON Schema + NativeResult.records 数据 |
| 工具/策略 | 专用工具、flash、无思考字段/规划/评判/压缩，每步3动作 | 原生工具、默认非flash及思考/规划/评判/压缩，每步5动作 |
| 任务/答案/评分 | 固定字段、数量、顺序和严格成功标准 | 同一份任务、答案和独立评分器 |

双方可能的总请求上限均为32，但分配不同：原生可自由用于任何功能，AutoScraper 主路径
另受24次子上限约束。这样不会遗漏原生 extract 等成本或将其挤入更小额度，但也不能说
预算分配完全相同；若预算绑定，必须报告，不归因纯粹架构优势。旧 Pilot 实际等待调用为0，
主请求仅7至12次。原生一次 run 的12 Step 与 AutoScraper 首轮相同，不关闭其原生预警。

保留默认原生能力意味着完整方案对照，不是只替换 Spec 的单因素消融：规划/评判可能
增加调用，evaluate 和更大 DOM/历史窗口也可能节省调用，差异不能仅归因于 Spec。
在相同单请求输出上限下，原生需交付实际数据、AutoScraper 只输出规则再由执行器取值；
其输出成本差异适用于此结构化 records 交付方式，不推广到全部文件交付策略。

历史 Pilot 与本轮不是同期随机配对；供应商缓存、网络和网页时序可能不同。缓存 Token
是输入子集，不再次相加。仅报告实际输入、输出、缓存及耗时，不估算未固定计价版本的金额。

## 使用与本轮边界

```bash
uv run --extra agent pytest tests/benchmark tests/observability/test_tracing.py -q
uv run python -m benchmarks audit --output output/native-baseline-audit.json
uv run --extra agent python -m benchmarks run \
  --config benchmarks/configs/native-pilot.json \
  --commit "$(git rev-parse HEAD)" --output-root output/native-baseline --allow-llm
```

固定干净 Commit 后，只对 Books 和 Load More 各真实运行一次。不自动重跑、不重新生成
AutoScraper 规则、不立即执行重复/大规模实验。凭据沿用环境，代码与生产隔离，无新依赖。
离线测试使用的脚本化响应仅在测试临时目录，不混入真实结果。

```bash
uv run python -m benchmarks score output/native-baseline/<批次>
```

通用摘要已改为中性说明，不再把原生组称为示例 Spec；该文案变更不放宽评分、不改写
任何既有 Pilot 的 summary、score 或原始文件。

## 条件成立后再做重复抓取

只有直接抓取与评分正常后，再批准并执行下列实验；本轮只设计，不运行。

1. 预先固定任务/真值/版本/模型/预算/profile，以 N=1、5、10 总交付次数作为同一序列
   的前缀，避免为每个 N 重复支付首次生成成本。
2. AutoScraper 从零真实生成和验证一次 Spec，第一次交付的全部成本计入；随后 N-1 次
   用该规则、新进程/浏览器，逐次独立评分。不能用手写/示例 Spec 代替生成成本，不能根据
   分数选择最好的首次生成；初次失败也保留在成本与交付成功率中。
3. 原生每次新 Agent/会话重新抓取，不提供上一轮记录、规则或轨迹。供应商缓存可能命中，
   每次保留真实缓存 Token，不以新会话假设没有缓存。
4. 按任务/重复轮次交替组序，保持来源版本一致。可先做探索性 N=5，再决定更多重复，
   不挑选有利结果；来源漂移时统一阻塞受影响比较。
5. 直接求和每次观测的请求、输入/输出/缓存 Token、墙钟与严格成功交付数：AutoScraper
   为首次生成交付 + 后续执行，原生为每次从零抓取。不用单次常数乘 N，不插值假造数据。
   重放失败不免费重建规则；如研究自愈，单独预注册实验组。

本轮仅与已保存的真实 Pilot 作历史参考，预先使用 Off 组作为首次生成成本，不事后选择
更便宜的 On/Off 记录。正式重复实验应同期重新采集生成与直抓，不据单次试跑宣称显著性。
