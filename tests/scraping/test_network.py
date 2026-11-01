# 网络抓取离线回归：真实浏览器请求提供完整 JSON，DOM 故意不展示库存等字段。
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from playwright.sync_api import sync_playwright

from autoscraper.browser.network import ResponseBuffer
from autoscraper.scraping.executor import ScraperExecutor
from autoscraper.scraping.network import (
    JsonPathError, NetworkError, NetworkProgressError, at_path, extract_json_record, matches,
)
from autoscraper.scraping.network_executor import ResponseTimeout
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.scraping.values import has_field_value, record_identity
from autoscraper.workflow.validation import validate_data
from autoscraper.workflow.requirements import TaskRequirements

URL = "https://fixture.test/"


def rule(**updates):
    data = json.loads(Path("examples/books.network.json").read_text())
    data.update(start_url=URL, timeout_ms=500, content_timeout_ms=250)
    data["network"]["url_pattern"] = URL + "api/items"
    data.update(updates)
    return ScraperSpec.model_validate(data)


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser.new_page()
        browser.close()


# 配置路由即可覆盖重复、空批次、错误与多个响应；每次仍由页面正常 fetch。
def setup(page, *, mode="normal", terminal=True):
    html = '''<main></main><button id="next">Next</button><script>
    let n=0; async function load() {
      const response = await fetch('/api/items?page='+(++n));
      const data = await response.json();
      document.querySelector('main').textContent = 'Book';
      if(n===2 && TERMINAL) document.querySelector('#next').disabled=true;
    } document.querySelector('#next').onclick=load; load();</script>'''
    html = html.replace("TERMINAL", "true" if terminal else "false")
    if mode == "missing":
        html = html.replace("const response = await fetch", "if(n===1) {document.querySelector('#next').disabled=true; return;} const response = await fetch")
    if mode == "multiple":
        html = html.replace("const data = await response.json();", "const data = await response.json(); await fetch('/api/items?page=3');")
    if mode == "unrelated":
        html = html.replace("load();</script>", "load(); fetch('/unrelated');</script>")
    def route(r):
        if "/api/items?" not in r.request.url:
            r.fulfill(content_type="text/html", body=html)
            return
        number = int(r.request.url.rsplit("=", 1)[1])
        item_id = 1 if mode == "repeat" else number
        items = [] if mode == "empty" and number == 2 else [
            dict(id=item_id, title="Book", stock=40+number, price=-1.25, active=False)
        ]
        if mode == "bad_record":
            items.insert(0, dict(id=99, title="Bad", price=0, active=False))
        body = json.dumps(dict(data=dict(items=items)))
        if mode == "invalid":
            body = "{invalid"
        if mode == "bad_path":
            body = '{"data": {"items": {}}}'
        if mode == "oversized":
            body = '"' + 'x'*1_000_001 + '"'
        r.fulfill(status=503 if mode == "http_error" else 200,
                  content_type="text/plain" if mode == "not_json" else "application/json", body=body)
    page.route("**/*", route)


def test_initial_and_click_extract_hidden_fields_without_dom_growth(page):
    setup(page)
    result = ScraperExecutor().run_page(rule(), page)
    assert [r["stock"] for r in result.records] == [41, 42]
    assert result.records[0]["price"] == -1.25 and result.records[0]["active"] is False
    assert result.pages_visited == 2 and result.stop_reason == "no_next"
    assert page.locator("main").inner_text() == "Book"
    assert not validate_data(result, TaskRequirements(fields=["stock", "price", "active"]))


@pytest.mark.parametrize("mode,error", [
    ("repeat", NetworkProgressError), ("missing", ResponseTimeout),
    ("invalid", NetworkError), ("bad_path", JsonPathError),
    ("not_json", NetworkError), ("http_error", NetworkError), ("oversized", NetworkError),
])
def test_network_errors_are_not_normal_exhaustion(page, mode, error):
    setup(page, mode=mode)
    with pytest.raises(error):
        ScraperExecutor().run_page(rule(), page)


@pytest.mark.parametrize("terminal", [True, False])
def test_empty_batch_requires_explicit_end(page, terminal):
    setup(page, mode="empty", terminal=terminal)
    if terminal:
        result = ScraperExecutor().run_page(rule(), page)
        assert len(result.records) == 1 and result.stop_reason == "no_next"
    else:
        with pytest.raises(NetworkProgressError):
            ScraperExecutor().run_page(rule(), page)


def test_multiple_and_unrelated_responses_limits_and_filtering(page):
    setup(page, mode="multiple")
    result = ScraperExecutor().run_page(rule(max_items=2), page)
    assert [r["id"] for r in result.records] == [1, 3] and result.stop_reason == "max_items"
    setup(page, mode="unrelated")
    result = ScraperExecutor().run_page(rule(max_pages=1), page)
    assert len(result.records) == 1 and result.stop_reason == "max_pages"
    setup(page)
    result = ScraperExecutor().run_page(rule(filters=[dict(field="stock", operator="gt", value=41)]), page)
    assert [r["stock"] for r in result.records] == [42] and result.filtered_out == 1


def test_skip_bad_field_and_listener_cleanup(page):
    setup(page, mode="bad_record")
    data = rule().model_dump()
    data["on_item_error"] = "skip"
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)
    assert result.error_details[0].code == "json_path_failure"
    assert len(result.records) == 2
    # 失败或成功退出均移除监听；同页重跑不会混入前一次缓存。
    setup(page)
    assert len(ScraperExecutor().run_page(rule(), page).records) == 2


@pytest.mark.parametrize("value", [0, -1, -0.25, False, True])
def test_json_required_scalar_values(value):
    source = rule().network.model_copy(update={"fields": {"x": rule().network.fields["id"].model_copy(update={"path": []})}})
    assert extract_json_record(value, source)["x"] == value
    assert has_field_value(value)
    assert record_identity({"x": False}, ["x"]) != record_identity({"x": 0}, ["x"])


def test_path_and_match_boundaries():
    source = rule().network
    assert matches(source, URL+"api/items?page=4&token=secret", "GET")
    assert not matches(source, URL+"api/items", "POST")
    ipv6 = source.model_copy(update={"url_pattern": "http://[::1]:8765/api/*"})
    assert matches(ipv6, "http://[::1]:8765/api/items?page=2", "GET")
    assert at_path({"a.b": [False]}, ["a.b", 0]) is False
    with pytest.raises(JsonPathError):
        at_path({"a": []}, ["a", 0])
    for change in [{"fields": {"x": {"selector": "p"}}}, {"version": "2"},
                   {"item_selector": "article"}, {"pagination": {"type": "link", "next_selector": "a"}}]:
        with pytest.raises(ValidationError):
            rule(**change)
    for path in [[-1], [True], ["a"]*33]:
        data = rule().model_dump()
        data["network"]["records_path"] = path
        with pytest.raises(ValidationError):
            ScraperSpec.model_validate(data)


def test_buffer_limits_and_safe_metadata(monkeypatch):
    import autoscraper.browser.network as network
    monkeypatch.setattr(network, "MAX_RESPONSES", 2)
    buffer = ResponseBuffer()
    request = SimpleNamespace(resource_type="fetch", url=URL+"api/items?token=private", method="GET", frame=SimpleNamespace(url=URL))
    for _ in range(3):
        entry = buffer.begin(request)
        buffer.finish(entry, b'[]')
    assert len(buffer.entries) == 2 and buffer.dropped == 1
    assert "private" not in json.dumps(entry.metadata())
    monkeypatch.setattr(network, "MAX_TOTAL_BYTES", 4)
    entry = buffer.begin(request)
    buffer.finish(entry, b'[12345]')
    assert entry.error == "body_limit" and entry.complete


# 链接翻页仍由真实导航触发请求；页数预算耗尽不能多访问下一页。
def test_network_link_pagination_and_budget(page):
    def route(r):
        if "/api/items?" in r.request.url:
            number = int(r.request.url.rsplit("=", 1)[1])
            r.fulfill(content_type="application/json", body=json.dumps(dict(data=dict(items=[
                dict(id=number, title="Book", stock=number, price=0, active=False),
            ]))))
        else:
            number = 2 if r.request.url.endswith("/2") else 1
            link = '<a href="/2">Next</a>' if number == 1 else ''
            r.fulfill(content_type="text/html", body=link +
                      f'<script>fetch("/api/items?page={number}")</script>')
    page.route("**/*", route)
    data = rule().model_dump()
    data["network"]["pagination"] = dict(type="link", next_selector="a")
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)
    assert [r["id"] for r in result.records] == [1, 2] and result.stop_reason == "no_next"
    data["max_pages"] = 1
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)
    assert page.url == URL and result.stop_reason == "max_pages"


# POST 和 XHR 同样来自浏览器实际请求，方法不匹配时不能误用其他响应。
def test_xhr_post_matching_and_request_failure(page):
    def route(r):
        if "/api/items" in r.request.url:
            if r.request.method == "POST":
                r.fulfill(content_type="application/json", body=json.dumps(dict(data=dict(items=[
                    dict(id=1, title="Book", stock=1, price=0, active=False),
                ]))))
            else:
                r.abort()
        else:
            r.fulfill(content_type="text/html", body='''<script>
                const xhr = new XMLHttpRequest(); xhr.open('POST', '/api/items'); xhr.send();
                fetch('/api/items');</script>''')
    page.route("**/*", route)
    data = rule().model_dump()
    data["network"].update(method="POST", pagination=None)
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)
    assert len(result.records) == 1 and result.stop_reason == "no_pagination"
    data["network"]["method"] = "GET"
    with pytest.raises(NetworkError, match="request_failed"):
        ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)


# response 事件只表示收到头部，不能在正文完成前提取或认定分页结束。
def test_delayed_response_body_is_awaited(page):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import time

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            is_api = self.path == "/api/items"
            body = (json.dumps(dict(data=dict(items=[
                dict(id=1, title="Book", stock=7, price=0, active=False),
            ]))) if is_api else '<script>fetch("/api/items")</script>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if is_api else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if is_api:
                self.wfile.write(body[:1])
                self.wfile.flush()
                time.sleep(0.15)
                body = body[1:]
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/"
        data = rule(start_url=base, content_timeout_ms=500).model_dump()
        data["network"].update(url_pattern=base+"api/items", pagination=None)
        assert ScraperExecutor().run_page(ScraperSpec.model_validate(data), page).records[0]["stock"] == 7
        data["content_timeout_ms"] = 30
        with pytest.raises(ResponseTimeout):
            ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# 同一个 article 原地替换标题，网络记录分批返回；支持页面与唯一容器两种滚动根。
def setup_scroll(page, *, container=False, delay=0, repeated=False, end=True,
                 loading_forever=False, tall=False, end_without_request=False):
    root = '<section id="scroller"><div id="height"><article>Book</article></div></section>'
    style = '#scroller {height:180px;overflow:auto} #height {height:500px}' if container else '#height {height:1300px}'
    html = f'''<style>{style} #loading,#end {{position:fixed;top:0}}</style>
    {root}<i id="loading" hidden>Loading</i><i id="end" hidden>End</i><script>
    let n=0, busy=false;
    const scrolling = CONTAINER ? document.querySelector('#scroller') : document.scrollingElement;
    async function load() {{
      busy=true; document.querySelector('#loading').hidden=false;
      if(n && FOREVER) return;
      await new Promise(resolve => setTimeout(resolve, DELAY));
      const data=await (await fetch('/api/items?page='+(++n))).json();
      document.querySelector('article').textContent = data.data.items[0].title;
      if(n===2 && END) document.querySelector('#end').hidden=false;
      if(TALL) document.querySelector('#height').style.height='100000px';
      document.querySelector('#loading').hidden=true; busy=false;
    }}
    (CONTAINER ? scrolling : window).addEventListener('scroll', () => {{
      if(!busy && scrolling.scrollTop + scrolling.clientHeight >= scrolling.scrollHeight-2) {{
        if(NO_REQUEST) document.querySelector('#end').hidden=false; else load();
      }}
    }});
    load();</script>'''
    for key, value in dict(CONTAINER=container, FOREVER=loading_forever, END=end, TALL=tall, NO_REQUEST=end_without_request).items():
        html = html.replace(key, 'true' if value else 'false')
    html = html.replace('DELAY', str(delay))
    requests = []
    def route(r):
        if '/api/items?' in r.request.url:
            number = int(r.request.url.rsplit('=', 1)[1])
            requests.append(number)
            r.fulfill(content_type='application/json', body=json.dumps(dict(data=dict(items=[
                dict(id=1 if repeated else number, title=f'Book {number}', stock=40+number,
                     price=0, active=False),
            ]))))
        else:
            r.fulfill(content_type='text/html', body=html)
    page.route('**/*', route)
    return requests


def scroll_rule(*, container=False, **updates):
    data = rule(content_timeout_ms=700, **updates).model_dump()
    data['network']['pagination'] = dict(type='scroll', max_scrolls=12, no_change_limit=2,
                                        loading_selector='#loading', end_selector='#end')
    if container:
        data['network']['pagination']['container_selector'] = '#scroller'
    return data


@pytest.mark.parametrize('container', [False, True])
def test_network_scroll_reads_new_json_with_fixed_dom_count(page, container):
    setup_scroll(page, container=container, delay=180)
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(scroll_rule(container=container)), page)
    assert [row['stock'] for row in result.records] == [41, 42]
    assert result.pages_visited == 2 and result.stop_reason == 'end_marker'
    assert page.locator('article').count() == 1


@pytest.mark.parametrize('repeated', [False, True])
def test_network_scroll_quiet_bottom_is_only_heuristic(page, repeated):
    setup_scroll(page, end=False, repeated=repeated)
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(scroll_rule()), page)
    assert result.stop_reason == 'no_more_content'
    assert len(result.records) == (1 if repeated else 2)
    assert result.pages_visited == (1 if repeated else 2)
    assert result.duplicates == (1 if repeated else 0)


@pytest.mark.parametrize('budget', ['scroll', 'pages'])
def test_network_scroll_budgets_do_not_claim_completion(page, budget):
    requests = setup_scroll(page, end=False, tall=True)
    data = scroll_rule(max_pages=1 if budget == 'pages' else 100)
    data['network']['pagination']['max_scrolls'] = 1
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)
    assert result.stop_reason == ('max_pages' if budget == 'pages' else 'scroll_limit')
    assert requests == [1]


def test_network_scroll_loading_timeout_is_not_quiet_exhaustion(page):
    setup_scroll(page, container=True, loading_forever=True, end=False)
    data = scroll_rule(container=True)
    data['content_timeout_ms'] = 180
    with pytest.raises(ResponseTimeout):
        ScraperExecutor().run_page(ScraperSpec.model_validate(data), page)


def test_network_scroll_missing_response_with_end_marker_is_explicit(page):
    requests = setup_scroll(page, container=True, end=False, end_without_request=True)
    # 仍须先捕获初始批次；滚动时的明确结束不要求站点额外发出空请求。
    result = ScraperExecutor().run_page(ScraperSpec.model_validate(scroll_rule(container=True)), page)
    assert result.stop_reason == 'end_marker' and requests == [1]


@pytest.mark.parametrize('timeout_ms', [100, 800])
def test_network_scroll_inflight_body_cannot_be_heuristic_end(page, timeout_ms):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit
    import time

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            number = int(parse_qs(parts.query).get('page', ['0'])[0])
            if number:
                body = json.dumps(dict(data=dict(items=[dict(id=number, title='Book', stock=number,
                                                           price=0, active=False)]))).encode()
            else:
                body = b'''<style>body{height:1500px}</style><main></main><i id="end" hidden>End</i>
                <script>let n=0,busy=false; async function load() {busy=true;
                const data=await (await fetch('/api/items?page='+(++n))).json();
                document.querySelector('main').textContent='Book'; busy=false;
                if(n===2) document.querySelector('#end').hidden=false;}
                window.addEventListener('scroll',()=>{const r=document.scrollingElement;
                if(!busy && r.scrollTop+r.clientHeight>=r.scrollHeight-2) load();});load();</script>'''
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if number else 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                if number == 2:
                    self.wfile.write(body[:1])
                    self.wfile.flush()
                    time.sleep(0.4)
                    body = body[1:]
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}/'
        data = scroll_rule(start_url=base)
        data['content_timeout_ms'] = timeout_ms
        data['network']['url_pattern'] = base+'api/items'
        data['network']['pagination'].pop('loading_selector')
        spec = ScraperSpec.model_validate(data)
        if timeout_ms == 100:
            with pytest.raises(ResponseTimeout):
                ScraperExecutor().run_page(spec, page)
        else:
            result = ScraperExecutor().run_page(spec, page)
            assert result.stop_reason == 'end_marker' and len(result.records) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# 网络预算截断仍导出已有数据，但入口必须标为部分结果；旧 DOM 返回码保持不变。
@pytest.mark.parametrize("network", [True, False])
@pytest.mark.parametrize("stop_reason", ["scroll_limit", "max_pages", "no_more_content"])
def test_network_budget_cli_returns_partial(tmp_path, monkeypatch, network, stop_reason):
    import sys
    from autoscraper.cli.scraper import main
    from autoscraper.scraping.executor import ScrapeResult

    data = scroll_rule() if network else dict(start_url=URL, item_selector="article",
                                             fields=dict(title=dict(selector="h1")))
    path = tmp_path / "rule.json"
    path.write_text(ScraperSpec.model_validate(data).model_dump_json())
    output = tmp_path / "data.json"
    monkeypatch.setattr(sys, "argv", ["autoscraper", str(path), "--output", str(output)])
    monkeypatch.setattr(ScraperExecutor, "run", lambda self, spec: ScrapeResult(
        records=[dict(title="Book")], stop_reason=stop_reason))
    assert main() == (2 if network and stop_reason != "no_more_content" else 0)
    assert json.loads(output.read_text()) == [dict(title="Book")]
