# Issue #21：认证状态传递验证

本次只验证浏览器之间的认证状态传递，不增加生产登录功能，不修改 ScraperSpec。
关联 [Issue #21](https://github.com/cloverfrog/AutoScraper/issues/21)。

真实模型的识别、接管与恢复行为另见 [Agent 人工接管实验](agent-handoff-probe.md)。

## 如何复现

安装项目的 agent 依赖和 Playwright Chromium 后执行：

```bash
uv run --extra agent python scripts/probe_auth_state.py --report output/auth-probe/report.json
```

可用 `--browser-path` 或 `AUTOSCRAPER_BROWSER_PATH` 指定完整 Chromium 可执行文件。
脚本不调用模型、不读取 API Key、不访问真实账号。HTTP 测试站点仅监听随机的
`127.0.0.1` 端口，每个场景使用独立站点和随机模拟 Token。认证状态快照只在内存传递，
报告只包含版本、状态和记录数；浏览器仍使用框架管理的临时 Profile。

## 验证方法

1. 由真实 browser-use `BrowserSession` 创建浏览器，Playwright 通过其 CDP 地址连接。
2. 在测试登录页点击按钮，模拟用户完成登录；确认受保护列表可访问。
3. 分别调用 browser-use 的公开导出接口、内部 CDP 导出接口，以及附着的 Playwright
   Context 的 `storage_state()`。三个接口读取同一浏览器的同一份认证状态。
4. 关闭原 browser-use 浏览器，再把每份状态交给全新、独立的 Playwright 浏览器。
   使用现有 `ScraperExecutor.run_page()` 和固定的 v2 规则执行一次 1 条试抓取、
   一次 2 条完整抓取；检查字段值、第二个列表页及独立详情 Page，不能仅凭未抛错判定成功。
5. 把 Playwright 快照载入全新的 browser-use 会话，模拟修复轮次的状态恢复。
6. 服务端在同一 Origin 废除原 Token，确认旧快照不能继续访问。

每种认证方式还使用空状态作为负对照。测试登录页返回 HTTP 200，因此未认证或过期时
能够暴露现有执行器等待内容超时的行为，而不会被 HTTP 错误提前截获。

## 本机结果（2026-10-30）

本次运行版本：browser-use `0.13.11`、Playwright `1.64.0`。

| 导出方式 | Cookie | localStorage | sessionStorage |
| --- | --- | --- | --- |
| `BrowserSession.export_storage_state()` | 成功 | 内容超时 | 内容超时 |
| `BrowserSession._cdp_get_storage_state()` | 成功 | 成功 | 状态格式被 Playwright 拒绝 |
| 通过 CDP 连接后调用 Playwright `context.storage_state()` | 成功 | 成功 | 内容超时 |
| 不载入认证状态 | 内容超时 | 内容超时 | 内容超时 |

“成功”要求试抓取得到 1 条、完整执行得到 2 条，均包含正确详情字段。
Cookie 和 localStorage 的 Playwright 快照也能恢复到新 browser-use 会话；
sessionStorage 未被该快照保存，恢复失败。废除 Token 后旧快照均返回内容超时。

公开 browser-use 导出接口实际把 `origins` 固定为空，因此不能传递 localStorage。
内部导出接口虽保存 sessionStorage，但仅包含 sessionStorage 的 Origin 缺少
Playwright 要求的 `localStorage` 数组，不能直接作为标准 storage state 加载。
即使补齐空数组，也不能假设 Playwright 会恢复非标准的 sessionStorage 字段。

## 对实施的建议

- 第一版采用标准 Playwright storage state 作为跨阶段快照。探索结束前通过 CDP
  连接 browser-use 浏览器导出；工作流持有快照，交给试抓取、正式执行和修复。
  本次连接和导出是可行性验证，生产实现仍应明确连接释放与浏览器所有权。
- 不依赖 browser-use 私有导出方法；公开接口目前也不足以满足 localStorage 场景。
- 每阶段结束应回收最新状态，避免继续使用已被服务端轮换的旧 Token。
- 明确首版对 sessionStorage 认证的限制，遇到实际站点后再评估额外恢复逻辑或共享会话。
- 新增明确的认证失败信号和有界人工恢复流程。当前“登录页返回 200”的路径仍只会
  得到 `content_timeout`；状态传递成功不代表认证失效识别已经实现。

本探针没有验证人工终端等待、继续/取消/超时、完整 LLM Agent 探索、真实登录网站、
IndexedDB、多域 SSO、Token 自动轮换、中途断点续抓、磁盘凭据权限或生产跟踪脱敏。
它也不证明所有站点都能迁移会话。上述项目应在正式实现和后续站点验收中单独覆盖。
