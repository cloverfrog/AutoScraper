# browser-use 追踪接入验证（Issue #14）

以下记录实现前的技术验证。正式追踪接口、隐私策略和使用方法见 [执行追踪](tracing.md)。

本次是 browser-use **0.13.11** 的离线组件验证，不实现正式追踪系统，不修改生产行为。
测试调用真实的工具分发、步骤执行、History 构建和 `add_new_task()` 方法；浏览器基础设施、
模型决策及无关的步骤处理使用替身。无需浏览器、网络或真实 API Key，不产生 LLM 费用。

## 复现

```bash
uv sync --extra agent --locked
uv run --extra agent pytest tests/observability/test_browser_use_tracing_probe.py -q
```

验证结果：10 项通过。测试中的记录器仅保存合成输入，不具备生产所需的脱敏、
大小限制、写入失败隔离和持久化能力，不应直接用于真实任务。

## 已确认的接入点

| 接入点 | 实际行为 | 实现建议 |
| --- | --- | --- |
| `run(on_step_start=..., on_step_end=...)` | `_execute_step()` 在步骤前后调用；结束时 `state.n_steps` 通常已加一 | 开始时保存 Step ID；结束时使用同一 ID，不能重新读取计数器作为当前步骤编号 |
| `register_new_step_callback` | `_handle_post_llm_processing()` 在 LLM 输出后调用，工具尚未执行 | 可记录计划动作；不能作为工具完成事件或工具结果来源 |
| `Tools.act()` | 内置 `navigate` 和项目 `ask_user` 均经过此入口；能获取输入和规范化后的返回值 | 使用每个任务独享的 Tools 子类包装入口，不做全局 monkeypatch；用单调时钟计时 |
| `Agent.multi_act()` | 导航的 `terminates_sequence` 标记会阻止后续排队动作执行 | 计划动作数与实际工具调用次数分开统计；在 `act()` 内记录实际调用 |
| `Agent._finalize()` / History | History 含步骤编号、起止时间、工具返回值、页面 URL 和状态消息 | 复用框架数据，但先筛选、脱敏和截断；不能原样作为默认日志导出 |
| `Agent.add_new_task()` | 保留 task ID、History 对象和步骤计数，重置暂停/停止标记，重建 EventBus | 澄清继续执行保持同一 Run / Attempt；每次调用 `run()` 继续传入步骤钩子 |

测试中的正常顺序为：`start(1) → plan(1) → tool(1) → end(2)`。
继续任务后为：`start(2) → plan(2) → tool(2) → end(3)`。
这里括号内是框架当时的 `state.n_steps`，不是建议使用的追踪 Step ID。

## 异常边界

- 普通工具异常经过 Registry / Tools 处理，最终返回 `ActionResult.error`，外层包装
  不一定能捕获原始异常。工具错误事件必须检查返回值，不能只依赖 `except`。
- `Tools.act(action_timeout=...)` 超时也返回 `ActionResult.error`。
- `CancelledError` 会穿过 `Tools.act()`，包装层可记录取消并原样抛出；计时清理放在
  `finally` 中。不能把取消吞掉转成正常工具返回值。
- `ask_user` 返回 `is_done=True, success=False`，但没有 `error`：这是结束当前动作
  批次以请求澄清，不能直接统计为工具异常。
- `_execute_step()` 自己处理的步骤超时仍触发 `on_step_end`；取消或从步骤边界逃出的
  异常会跳过该钩子。它不是 `finally` 钩子，Run / Attempt 外层仍需兜底记录未闭合步骤。
- 回调本身的异常没有通用隔离保护。正式实现应在自身事件接口中隔离消费者写入失败，
  防止日志错误影响 Agent 执行；本次未实现或验证这套机制。

## History 与恢复执行的限制

真实 History 构建测试确认：`result.extracted_content`、`state_message` 及 URL 查询
参数会保留原文，包括测试放入的模拟敏感值。默认落盘需要字段白名单，且复用同一套
脱敏和大小限制策略。不要直接调用原始 History 导出方法后假设文件已安全。

组件测试通过真实 `add_new_task()` 后再次执行 `_execute_step()` 验证计数延续。
另外，在现有 `tests/agent/test_clarification.py::test_real_agent_continues_with_browser_and_history`
中补充步骤回调断言：使用本机 HTTP 页面、真实 Chromium 和脚本化 LLM 响应，经过导航、
页面检查、澄清、回答和恢复完成，实际调用两次 `Agent.run()`。确认两次 task ID 相同，
第二次从第 4 步继续，保留前 3 条 History，4 个步骤均按 start / plan / end 顺序触发。
回答由测试处理器提供，不是真实人工输入。

```bash
uv run --extra agent pytest tests/observability/test_browser_use_tracing_probe.py tests/agent/test_clarification.py -q
```

上述组合回归需安装 Chromium，并允许本机回环 HTTP 服务及浏览器进程；LLM 不联网。
源码还显示 `run()` 的循环使用
`state.n_steps <= max_steps`，因此恢复执行时 `max_steps` 是同一 Agent 的累计上限，
不是额外分配的步骤数。这一点应在未来端到端测试中一起覆盖。

## 后续实现方向

核心层提供轻量事件接口和默认空实现；Run / Attempt / Step 上下文由工作流维护，
步骤钩子负责正常步骤边界，Tools 包装负责真实调用，LLM 适配层负责请求耗时与 Usage。
外层退出处理补齐取消、异常和未闭合步骤，消费者负责 JSONL、脱敏和失败隔离。

此次验证支持以上接入方案的可行性，但尚未覆盖 LLM Usage、日志并发隔离、磁盘失败、
真实浏览器导航失败、多轮修复、真实人工输入及 CLI stdout/退出码兼容性；这些仍属于
Issue #14 后续实现与验收范围。

对应框架源码：`browser_use/agent/service.py` 的 `_execute_step`、
`_handle_post_llm_processing`、`step`、`_finalize`、`_make_history_item`、
`multi_act`、`add_new_task`、`run`，以及 `browser_use/tools/service.py` 的 `act`
和 `browser_use/tools/registry/service.py` 的 `execute_action`。
