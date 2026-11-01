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


# 滚动示例保留同一个 main，仅更新文本；API 的新增记录不依赖 DOM 节点数量。
SCROLL_HTML = '''<!doctype html><meta charset="utf-8"><title>Network scroll books</title>
<style>body {min-height:1500px} #loading,#end {position:fixed;top:0}</style>
<main></main><i id="loading" hidden>Loading</i><i id="end" hidden>End</i><script>
let page=0, busy=false, more=true;
async function load() {
  busy=true; document.querySelector('#loading').hidden=false;
  await new Promise(resolve => setTimeout(resolve, 100));
  const data=await (await fetch('/api/items?page='+(++page))).json();
  document.querySelector('main').textContent=data.data.items.map(x => x.title).join(', ');
  more=data.has_more; document.querySelector('#end').hidden=more;
  document.querySelector('#loading').hidden=true; busy=false;
}
window.addEventListener('scroll', () => {
  const root=document.scrollingElement;
  if(!busy && more && root.scrollTop+root.clientHeight >= root.scrollHeight-2) load();
});
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
            body = (SCROLL_HTML if parts.path == "/scroll" else HTML).encode()
            content_type = "text/html; charset=utf-8"
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
