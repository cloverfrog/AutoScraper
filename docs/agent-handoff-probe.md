# Issue #21：Agent 人工接管实验

本次关注真实 Agent 能否识别登录阻塞、请求用户操作、在操作后继续原任务。
这与 [认证状态传递探针](auth-state-probe.md) 是两个独立实验。

## 实验设计

使用现有 `generate_spec()`、browser-use Agent、DeepSeek Responses 适配器和提交检查。
模型为配置默认的 `deepseek-flash`，browser-use `0.13.11`、Playwright `1.64.0`。
不模拟模型输出或替 Agent 生成选择器、决定导航、提交规则。

测试站点仅监听本地随机端口。任务要求 Research 工作空间的两条报告，字段为
`title`、`price`，明确排除 Personal 工作空间。两个工作空间的列表具有相同的
字段和 DOM 结构，因此“选择器有效、字段齐全、数量正确”不能证明工作空间正确。
提交后由独立 Executor 验证具体数据必须是 Research Alpha/$10 和 Research Beta/$20。

基线保留现有提示词及工具。实验组仅在脚本内做两项适配：

- 把禁止登录的边界改为禁止 Agent 自动登录，允许请求人工接管。
- 注册实验性 `wait_for_user(reason)`，利用现有 ClarificationSession 的终止动作批次、
  同浏览器/同历史继续机制传输请求。同步处理器在另一个线程内模拟用户改 Cookie、
  导航页面，然后仅回复 `Continue.`。模型不知道预设的场景名称和落点。

真实终端用户等待和生产认证机制没有实现。对照同时改变了提示词和工具可用性，
不能把差异单独归因于其中一项。探索绕过工作流的需求解析，直接使用明确的字段/数量约束。

每个场景最多 12 次主模型调用，最多执行两次模拟用户操作；第三次接管请求不再得到
回答，通过现有 `ClarificationRequired` 结束。保留现有总探索时间和步骤预算。

## 实测结果（2026-10-30）

五个场景各运行一次，共 26 次主模型调用，没有语义等待模型调用。

| 场景 | 主模型调用 | 观察到的行为 | 独立抓取结果 |
| --- | --- | --- | --- |
| 当前 Agent，未增加接管能力 | 5 | 检查登录墙，尝试替代 URL，报告 unsupported | 未提交规则 |
| 人工登录后回到目标列表 | 4 | 主动请求接管；继续后 inspect_page，再提交规则 | 两条 Research 数据正确 |
| 人工登录后停留首页 | 6 | 点击 Research 入口；检查并测试字段后提交 | 两条 Research 数据正确 |
| 仍未登录，但用户选择继续 | 5 | 共请求接管三次，中间尝试点击登录链接；第三次由脚本上限停止 | 未误提交规则，needs_human |
| 人工登录后停留 Personal 工作空间 | 6 | 点击 Research 入口，重新检查并测试字段 | 两条 Research 数据正确 |

工具动作顺序如下。初始 navigate 是 Agent 初始化导航，不计作人工干预：

```text
baseline: navigate → inspect_page → navigate → navigate → report_unsupported
target:   navigate → wait_for_user → [用户操作] → inspect_page → done
home:     navigate → wait_for_user → [用户操作] → click → inspect_page → test_fields → done
wrong:    navigate → wait_for_user → [用户操作] → click → inspect_page → test_fields → done
blocked:  navigate → wait_for_user → [继续] → wait_for_user → [继续] → click → wait_for_user → [无回答]
```

目标列表场景没有显式调用 test_fields；现有 done 提交检查自动刷新列表字段证据，
之后独立 Executor 也确认了结果。不能把程序自动检查描述成模型主动完成的测试。
首页和错误工作空间场景恢复后首先是 click，基于 browser-use 提供的新页面信息，
随后才调用 inspect_page；“必须先显式调用 inspect_page”不是本次成功的必要条件。
动作记录中的接管次数也表明，wait_for_user 之后没有继续执行动作，直到处理器返回。

## 对 Agent 挑战的判断

1. 对有明确登录提示、明确工作空间和入口链接的简单页面，识别阻塞、请求接管和
   恢复导航没有表现为主要障碍。当前模型配合很少的提示词调整与现成继续机制就能完成。
2. 页面变化不等于一定要重建 Agent；保留任务和历史，同时让框架取得新浏览器状态，
   在本次场景中已经足够。跨标签页、旧 DOM 缓存及复杂页面尚未验证。
3. 未登录却继续时，模型没有把“继续”当作登录成功，但会持续请求人工操作。
   本次是程序上限结束流程，并非模型自行判断不再尝试。正式实现需要有界重试和
   无进展、取消、超时等明确结束条件，不能仅依赖提示词。
4. 工作空间范围仍是语义约束。现有提交检查不能通用地证明账号、租户和权限正确；
   本次通过固定预期数据独立评分，证明这一次选对了范围，不代表能保证所有站点。

这些结果只说明简单场景可行，不能估算成功率。尚未覆盖隐蔽登录墙、登录后权限不足、
多标签页切换、长时间等待、真实验证码/SSO、中途认证过期或页面信息误导等情况。

## 复现

需要已配置 DeepSeek API Key 和安装 agent 依赖、Playwright Chromium。
下述命令会产生模型费用，默认最多 5 × 12 次主模型请求；不读取真实站点账号。

```bash
uv run --extra agent python scripts/probe_agent_handoff.py
```

可以通过 `--scenario home wrong_workspace` 只跑选定场景。每场景 JSON 写入
`output/agent-handoff-probe/`，包括动作参数、工具返回、接管请求、HTTP 访问记录、
候选规则和独立数据验证结果，不包含 API Key 或认证状态快照。

`--resume-policy reobserve` 可额外测试明确要求重新观察的恢复提示词；本报告只运行了
默认的 minimal 策略，不提供两种恢复提示词之间的效果比较。
