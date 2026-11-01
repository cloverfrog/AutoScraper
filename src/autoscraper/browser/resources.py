# 探索只记录响应头证据，不读取正文、不保存文件；独立于 JSON 正文缓存。
"""Bounded header-only evidence for browser-loaded resources."""

from collections import deque


# 同一次浏览器请求的重定向链保留原 URL，支持记录指向入口、正文来自 CDN 的情况。
def request_urls(request):
    urls = set()
    while request is not None and len(urls) < 20:
        if request.url in urls:
            break
        urls.add(request.url)
        request = request.redirected_from
    return urls


class ResourceEvidence:
    def __init__(self, context):
        self.context = context
        self.entries = deque(maxlen=200)
        self.probes = 0
        context.on("response", self.responded)

    def responded(self, response):
        try:
            if response.request.method == "GET":
                self.entries.append(dict(
                    urls=request_urls(response.request), page=response.request.frame.page.url,
                    status=response.status,
                    content_type=response.headers.get("content-type", "").split(";", 1)[0][:100],
                ))
        except Exception:
            pass

    def close(self):
        self.context.remove_listener("response", self.responded)
        self.entries.clear()
