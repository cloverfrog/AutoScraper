# 仅观察浏览器正常发起的 Fetch/XHR；有限缓存不保存请求头、Cookie 或请求正文。
# 同步执行器与异步探索器使用相同缓存和解析策略，监听必须在导航/点击之前安装。
"""Small bounded response collectors; never send or replay HTTP requests."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import json
from time import monotonic

from autoscraper.scraping.network import endpoint, matches

MAX_BODY_BYTES = 1_000_000
MAX_TOTAL_BYTES = 4_000_000
MAX_RESPONSES = 40


@dataclass
class CapturedResponse:
    id: int
    url: str
    method: str
    page_url: str = ""
    status: int = 0
    content_type: str = ""
    resource_type: str = ""
    body: object = None
    error: str | None = None
    complete: bool = False
    size: int = 0

    def metadata(self):
        return dict(id=self.id, url=endpoint(self.url)[:2048], method=self.method,
                    status=self.status, content_type=self.content_type[:100],
                    resource_type=self.resource_type, complete=self.complete, error=self.error)


class ResponseBuffer:
    def __init__(self, source=None):
        self.source = source
        self.sequence = 0
        self.entries = OrderedDict()
        self.total_bytes = 0
        self.dropped = 0

    # 以请求开始序号确定动作边界，防止点击前尚未完成的旧请求冒充新批次。
    def begin(self, request):
        if request.resource_type not in {"fetch", "xhr"}:
            return None
        if self.source is not None and not matches(self.source, request.url, request.method):
            return None
        self.sequence += 1
        if len(self.entries) >= MAX_RESPONSES:
            _, old = self.entries.popitem(last=False)
            self.total_bytes -= old.size
            self.dropped += 1
        entry = CapturedResponse(self.sequence, request.url, request.method,
                                 resource_type=request.resource_type)
        try:
            entry.page_url = request.frame.url
        except Exception:
            pass
        self.entries[entry.id] = entry
        return entry

    # Content-Length 只能提前排除已知大响应；读取后再次检查实际解码字节数。
    def headers(self, entry, response):
        entry.status = response.status
        headers = response.headers
        entry.content_type = headers.get("content-type", "").split(";", 1)[0].lower()
        if not 200 <= entry.status < 300:
            entry.error = "http_status"
        elif entry.content_type != "application/json" and not entry.content_type.endswith("+json"):
            entry.error = "not_json"
        try:
            if int(headers.get("content-length", "0")) > MAX_BODY_BYTES:
                entry.error = "body_limit"
        except ValueError:
            pass

    def finish(self, entry, raw):
        if entry.id not in self.entries:
            return
        if len(raw) > MAX_BODY_BYTES or self.total_bytes + len(raw) > MAX_TOTAL_BYTES:
            entry.error = "body_limit"
        else:
            try:
                entry.body = json.loads(raw)
                entry.size = len(raw)
                self.total_bytes += entry.size
            except (ValueError, UnicodeError, RecursionError):
                entry.error = "invalid_json"
        entry.complete = True

    def clear(self):
        self.entries.clear()
        self.total_bytes = 0
        self.dropped = 0


class SyncResponseCapture:
    def __init__(self, page, source):
        self.page = page
        self.buffer = ResponseBuffer(source)
        self.pending = {}
        self.listeners = {"request": self.requested, "response": self.responded,
                          "requestfinished": self.finished, "requestfailed": self.failed}

    def start(self):
        for event, callback in self.listeners.items():
            self.page.on(event, callback)
        return self

    def requested(self, request):
        entry = self.buffer.begin(request)
        if entry:
            self.pending = {r: e for r, e in self.pending.items() if e.id in self.buffer.entries}
            self.pending[request] = entry

    def responded(self, response):
        entry = self.pending.get(response.request)
        if entry:
            self.buffer.headers(entry, response)

    # requestfinished 表示响应体已收完；回调只在浏览器事件循环内读取，不创建后台线程。
    def finished(self, request):
        entry = self.pending.pop(request, None)
        if entry is None:
            return
        try:
            if entry.error:
                entry.complete = True
            else:
                response = request.response()
                if response is None:
                    raise ValueError
                self.buffer.finish(entry, response.body())
        except Exception:
            entry.error, entry.complete = "body_unavailable", True

    def failed(self, request):
        entry = self.pending.pop(request, None)
        if entry:
            entry.error, entry.complete = "request_failed", True

    def close(self):
        for event, callback in self.listeners.items():
            self.page.remove_listener(event, callback)
        self.pending.clear()
        self.buffer.clear()


class AgentResponseCapture:
    def __init__(self):
        self.buffer = ResponseBuffer()
        self.pending = {}
        self.tasks = set()
        self.playwright = None
        self.context = None
        self.evidence = set()
        self.actions = []

    # browser.start 只创建空白浏览器；接入上下文监听后才允许 Agent 执行首次导航。
    async def start(self, browser):
        from playwright.async_api import async_playwright

        await browser.start()
        self.playwright = await async_playwright().start()
        attached = await self.playwright.chromium.connect_over_cdp(browser.cdp_url)
        if not attached.contexts:
            raise RuntimeError("Network capture requires an existing browser context")
        self.context = attached.contexts[0]
        self.context.on("request", self.requested)
        self.context.on("response", self.responded)
        self.context.on("requestfinished", self.finished)
        self.context.on("requestfailed", self.failed)

    def requested(self, request):
        entry = self.buffer.begin(request)
        if entry:
            # 请求引用也有界，移除已淘汰的候选，避免长任务累积未完成请求。
            self.pending = {r: e for r, e in self.pending.items() if e.id in self.buffer.entries}
            self.pending[request] = entry

    def responded(self, response):
        entry = self.pending.get(response.request)
        if entry:
            self.buffer.headers(entry, response)

    def finished(self, request):
        entry = self.pending.pop(request, None)
        if entry is not None:
            if len(self.tasks) >= MAX_RESPONSES:
                entry.error, entry.complete = "body_read_limit", True
                return
            task = asyncio.create_task(self.read(request, entry))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def read(self, request, entry):
        try:
            if entry.error:
                entry.complete = True
            else:
                response = await request.response()
                if response is None:
                    raise ValueError
                raw = await asyncio.wait_for(response.body(), timeout=5)
                self.buffer.finish(entry, raw)
        except Exception:
            entry.error, entry.complete = "body_unavailable", True

    def failed(self, request):
        entry = self.pending.pop(request, None)
        if entry:
            entry.error, entry.complete = "request_failed", True

    # 工具测试等待已选响应读完；不把 requestfinished 回调的异步调度延迟误判成路径失败。
    async def settle(self, ids, timeout_ms):
        deadline = monotonic() + timeout_ms / 1000
        while any(not entry.complete for key, entry in self.buffer.entries.items() if key in ids):
            if monotonic() >= deadline:
                return
            await asyncio.sleep(min(0.02, max(0, deadline - monotonic())))

    async def close(self):
        if self.context:
            for event, callback in {"request": self.requested, "response": self.responded,
                                    "requestfinished": self.finished,
                                    "requestfailed": self.failed}.items():
                self.context.remove_listener(event, callback)
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.playwright:
            # 只断开旁路连接，实际浏览器仍由 browser-use 回收。
            await self.playwright.stop()
        self.pending.clear()
        self.buffer.clear()
