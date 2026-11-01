# 本地网络数据源示例：页面仅展示标题，库存、小数价格和布尔值只存在于 JSON。
# 启动后可用 Agent 探索，也可直接执行 books.network.json；不需要外部 API 或模型密钥。
"""Run: python examples/network_demo.py [--port 8765]."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import parse_qs, urlsplit

HTML = '''<!doctype html><meta charset="utf-8"><title>Network books</title>
<main></main><button id="next" disabled>Next</button><script>
let page = 0;
async function load() {
  document.querySelector('#next').disabled = true;
  const data = await (await fetch('/api/items?page='+(++page))).json();
  document.querySelector('main').textContent = data.data.items.map(x => x.title).join(', ');
  document.querySelector('#next').disabled = !data.has_more;
}
document.querySelector('#next').onclick = load;
load();
</script>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/items":
            number = int(parse_qs(parts.query).get("page", ["1"])[0])
            body = json.dumps({"data": {"items": [
                {"id": number, "title": f"Book {number}", "stock": 40 + number,
                 "price": -1.25 if number == 1 else 0, "active": False},
            ]}, "has_more": number < 2}).encode()
            content_type = "application/json"
        else:
            body, content_type = HTML.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
