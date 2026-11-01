# 资源保存只由独立执行器显式启用；两种模式共用预算、文件命名与记录清单。
# 响应监听只保留有界引用，正文在匹配到保留记录之后读取，不复用 JSON 缓存。
"""Optional record-bound resource saving, without automatic mode switching."""

from collections import OrderedDict
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
from time import monotonic
from urllib.parse import urldefrag, urljoin, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from autoscraper.browser.resources import request_urls


@dataclass(frozen=True)
class ResourceOptions:
    directory: Path
    max_files: int = 200
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    timeout_ms: int = 5000
    max_per_record: int = 20

    def __post_init__(self):
        if any(value <= 0 for value in (
            self.max_files, self.max_file_bytes, self.max_total_bytes,
            self.timeout_ms, self.max_per_record,
        )):
            raise ValueError("Resource limits must be positive")


# 完整 URL 仅在运行内参与匹配；清单和错误不包含地址、签名、请求头或异常原文。
def resource_url(value, base):
    if not isinstance(value, str) or not value.strip():
        return None
    url = urldefrag(urljoin(base, value.strip())).url
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        return None
    return url


class ResourceSaver:
    def __init__(self, context, options, rules, result):
        self.context, self.options, self.rules, self.result = context, options, rules, result
        Path(options.directory).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="resources-", dir=options.directory))
        result.resource_directory = str(self.directory)
        self.entries = OrderedDict()
        self.complete = set()
        self.total_bytes = 0
        self.attempts = 0
        self.overflow = False
        self.listeners = {"request": self.requested, "response": self.responded, "requestfinished": self.finished,
                          "requestfailed": self.finished}
        if any(rule.mode == "response" for rule in rules):
            for event, callback in self.listeners.items():
                context.on(event, callback)

    # 链接分页和详情导航离开旧文档后，旧响应不能冒充新文档中的同 URL 资源。
    def requested(self, request):
        try:
            if not request.is_navigation_request() or request.frame != request.frame.page.main_frame:
                return
            for old in list(self.entries):
                if old.frame.page == request.frame.page:
                    self.entries.pop(old)
                    self.complete.discard(old)
        except Exception:
            pass

    # 候选只保留响应引用，禁止缓存所有二进制正文；淘汰后按未观察到报告。
    def responded(self, response):
        try:
            if response.request.method != "GET":
                return
            if len(self.entries) >= 500:
                request, _ = self.entries.popitem(last=False)
                self.complete.discard(request)
                self.overflow = True
            self.entries[response.request] = response
        except Exception:
            pass

    def finished(self, request):
        if request in self.entries:
            self.complete.add(request)

    # 以页面与精确 URL 关联，不按响应到达顺序或去掉查询串后的路径猜测。
    def response_for(self, page, url):
        deadline = monotonic() + self.options.timeout_ms / 1000
        while True:
            matches = []
            for request, response in list(self.entries.items()):
                try:
                    if (not 300 <= response.status < 400 and url in request_urls(request)
                            and request.frame.page == page):
                        matches.append((request, response))
                except Exception:
                    continue
            if len(matches) > 1:
                return None, "ambiguous_response"
            if matches:
                response = matches[0][1]
                try:
                    too_large = int(response.headers.get("content-length", "0")) > min(
                        self.options.max_file_bytes, self.options.max_total_bytes - self.total_bytes,
                    )
                except ValueError:
                    too_large = False
                if response.status >= 400 or too_large:
                    return response, None
            if matches and matches[0][0] in self.complete:
                return matches[0][1], None
            if monotonic() >= deadline:
                return None, "capture_limit" if self.overflow else "response_timeout"
            page.wait_for_timeout(20)

    # 记录已通过筛选和去重；序号对应最终 JSON/CSV 行，数组位置保证多个资源不串位。
    def save_record(self, record, index, page, detail_page=None, detail_fields=()):
        count = 0
        for rule in self.rules:
            value = record.get(rule.field)
            values = value if isinstance(value, list) else [value]
            owner = detail_page if rule.field in detail_fields else page
            for position, value in enumerate(values, 1):
                count += 1
                entry = dict(record=index, field=rule.field, position=position, path=None)
                if count > self.options.max_per_record:
                    entry["status"] = "record_limit"
                    self.result.resources.append(entry)
                    break
                if self.attempts >= self.options.max_files:
                    entry["status"] = "file_limit"
                elif self.total_bytes >= self.options.max_total_bytes:
                    entry["status"] = "total_limit"
                elif value is None or value == "":
                    entry["status"] = "missing"
                else:
                    self.attempts += 1
                    try:
                        url = resource_url(value, owner.url)
                        entry.update(self.save_one(owner, url, rule.mode))
                    except Exception:
                        # Playwright 错误可能含完整 URL 或请求头，不能进入普通日志。
                        entry["status"] = "resource_failed"
                self.result.resources.append(entry)

    def save_one(self, page, url, mode):
        if url is None:
            return dict(status="unsupported_url")
        response = None
        try:
            if mode == "response":
                response, error = self.response_for(page, url)
                if error:
                    return dict(status=error)
            else:
                response = self.context.request.get(url, timeout=self.options.timeout_ms)
            if not 200 <= response.status < 300:
                return dict(status="http_error", http_status=response.status)
            headers = response.headers
            limit = min(self.options.max_file_bytes,
                        self.options.max_total_bytes - self.total_bytes)
            try:
                if int(headers.get("content-length", "0")) > limit:
                    return dict(status="size_limit")
            except ValueError:
                pass
            # body() 返回完整解码字节；这是保存上限，不是网络或进程内存硬上限。
            body = response.body()
            if len(body) > limit:
                return dict(status="size_limit")
            content_type = headers.get("content-type", "").split(";", 1)[0].lower()
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                      "image/gif": ".gif", "application/pdf": ".pdf",
                      "text/plain": ".txt"}.get(content_type, ".bin")
            name = f"{self.attempts:06d}{suffix}"
            # 独立运行目录和生成文件名避免覆盖、目录穿越与服务端文件名冲突。
            target = self.directory / name
            try:
                with target.open("xb") as stream:
                    stream.write(body)
            except OSError:
                target.unlink(missing_ok=True)
                raise
            self.total_bytes += len(body)
            return dict(status="saved", path=name, bytes=len(body), content_type=content_type[:100])
        except PlaywrightTimeout:
            return dict(status="request_timeout")
        finally:
            if mode == "request" and response is not None:
                response.dispose()

    def close(self):
        for event, callback in self.listeners.items():
            self.context.remove_listener(event, callback)
        self.entries.clear()
        self.complete.clear()
        (self.directory / "manifest.json").write_text(
            json.dumps(self.result.resources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
