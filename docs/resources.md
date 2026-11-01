# 图片与普通附件资源

资源规则与 DOM v1/v2、Network v3 共用。协议描述资源定位和获取模式；是否保存是本次独立执行的选项，默认关闭。
不需要为保存文件重新调用 Agent，也不增加资源专属的分页或滚动流程。

## 协议示例

```json
{
  "version": "1",
  "start_url": "https://example.com/products",
  "item_selector": ".product",
  "fields": {
    "id": {"selector": ":scope", "extract": "attribute", "attribute": "data-id"},
    "images": {"selector": "img", "extract": "attributes", "attribute": "src", "transform": "absolute_url"},
    "attachment": {"selector": "a.attachment", "extract": "attribute", "attribute": "href", "transform": "absolute_url", "required": false}
  },
  "resources": [
    {"field": "images", "mode": "response"},
    {"field": "attachment", "mode": "request"}
  ],
  "dedupe_by": ["id"],
  "max_items": 100
}
```

`resources` 最多 20 项，字段必须存在且不能重复引用。字段值支持 URL 字符串、字符串列表或 null。
DOM 的 `attributes` 按匹配顺序提取多个属性，忽略缺失属性；允许 `absolute_url` 转换，空集合得到 `[]`。
单资源继续使用 `attribute`。详情资源引用 `detail.fields` 中的字段。
Network 资源引用 `network.fields`；现有 JSON 路径可直接选取字符串数组，不混入 DOM 字段。
相对地址相对于记录所属列表/详情页面解析。

- `request`：在当前 BrowserContext 的 request 中重新 GET，共享 Cookie。不复制图片请求上下文、私有请求头或动态签名。
- `response`：在导航前监听浏览器正常加载的响应，保留最多 500 个响应引用，不缓存所有资源正文。
  记录通过筛选、去重后，在离开其页面前读取匹配响应并保存。以所属 Page 与完整 URL（含查询串）关联，忽略 fragment。
  正常 GET 重定向通过同一次请求链关联回原 URL（最多 20 跳）。
  同一文档中出现多个相同 URL 的响应会报告歧义，不按到达顺序猜测；新文档导航会清理该页面的旧引用。

两种模式不会自动切换。响应引用淘汰、正文不可读、浏览器未加载目标、URL 不可重放时，明确记录失败。
响应模式可覆盖一次性地址或依赖原始图片请求上下文的资源；并不保证浏览器总能提供历史响应正文。

## 独立执行与产物

```bash
# 默认：只导出数据和 URL
uv run autoscraper output/rule.json --output output/data.json

# 显式保存资源；每次创建独立运行目录
uv run autoscraper output/rule.json --output output/data.json --output output/data.csv \
  --download-resources output/assets
```

CLI 摘要包含 `resource_directory` 与 `resource_errors`。资源失败仍保留记录，退出码为 2；未声明资源的规则不创建目录。
UI 在“执行已有规则”页勾选保存开关后，提供包含清单和文件的 `resources.zip`。
Agent 页即使启用“完整抓取”，也不启用资源保存。

`manifest.json` 的每项包含：

```json
{"record": 1, "field": "images", "position": 1, "path": "000001.png", "status": "saved", "bytes": 1234, "content_type": "image/png"}
```

`record` 是最终 JSON 数组/CSV 数据行的 **1 起始序号**（不含 CSV 表头），`position` 是字段内资源位置。
`path` 相对于清单目录。生成文件名不使用服务端文件名，独立目录避免运行之间覆盖。
原记录的 URL 字段与去重规则保持原语义；正文不进入记录、LLM、普通追踪日志或错误原文。
清单不重复保存 URL、查询签名或请求头。用户显式提取的原始 URL 字段仍会导出，应自行管理这些业务数据。

默认限制：每运行最多 200 次有效资源尝试、每记录 20 项、单文件 10 MiB、保存总量 100 MiB、等待/请求超时 5000 ms。
CLI 可通过 `--resource-max-files`、`--resource-max-file-bytes`、`--resource-max-total-bytes`、`--resource-timeout-ms` 调整；
Python API 使用 `ScraperExecutor(resources=ResourceOptions(directory=...))`，还可调整 `max_per_record`。
数量预算包括失败尝试；缺失值不发请求。空数组没有清单项，null/空字符串记录为 `missing`。
失败状态包括 `http_error`、`request_timeout`、`response_timeout`、`ambiguous_response`、`capture_limit`、`size_limit`、`file_limit`、
`record_limit`、`total_limit`、`unsupported_url` 和不暴露异常细节的 `resource_failed`。

大小是**保存上限**：先按 Content-Length 预检，再校验实际解码字节。Playwright 的 `body()` 会返回完整字节，
请求模式的 GET 也可能在返回前已接收正文，因此不保证限制网络流量或进程峰值内存。
响应等待限制等待目标出现和请求完成；完成后 `body()` 的底层读取没有独立的 Playwright 超时参数。
首版不引入流式下载/CDP 框架来提供这些硬限制。

## Agent 证据与能力边界

自然语言要求保存文件时，需求模型单独保留 `resource_fields`，提交必须包含对应资源规则，避免仅输出 URL 就宣称完成。
`test_resources(spec)` 按精确候选字段最多检查三条当前记录、每字段最多三个 URL：

- 响应模式仅检查正常 GET 响应的状态与类型，最多保留 200 项元数据。
- 请求模式每次探索最多进行六次 HEAD 探测，单次 3000 ms，共享当前 Cookie，不读取资源正文。
- 探测通过仅代表轻量证据成立，不保证后续完整 GET 成功。只支持 GET、拒绝 HEAD 的站点暂时不能通过请求模式探索验证。
- 详情资源需在相应详情页测试；字段定位、数据源、模式改变后需重新测试。

探索阶段不会批量下载、保存资源文件或把二进制正文送给模型。Agent 内部的试抓取、独立验证、修复与完整执行都只提取数据/URL。
主动点击下载、POST 附件、动态生成 blob、未加载且不可重放的资源、视频流和 Canvas 还原均不在首版范围。

## 验证

`tests/scraping/test_resources.py` 使用本机 HTTP 服务和真实 Chromium，覆盖 DOM/Network 的链接、点击与容器滚动、
多资源与附件关联、一次性且依赖图片请求上下文的资源、默认关闭、404、超限、CLI/UI 产物及 Agent 禁止读正文。
`tests/workflow/test_workflow.py` 另验证保存需求不能被 URL 字段替代，以及 Agent 工作流不初始化资源保存器。
