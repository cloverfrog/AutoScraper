# 真实本机 HTTP 与 Chromium 验证资源关联、上下文、分页及显式下载边界。
import base64
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from pydantic import ValidationError
import pytest

from autoscraper.scraping.executor import ScraperExecutor
from autoscraper.scraping.resources import ResourceOptions
from autoscraper.scraping.spec import ScraperSpec

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


@pytest.fixture
def site():
    hits = Counter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            query = parse_qs(urlsplit(self.path).query)
            hits[(self.command, path)] += 1
            status, kind, body = 200, 'image/png', PNG
            if path == '/':
                n = query.get('page', ['1'])[0]
                body = '''<style>#pane{height:100px;overflow:auto}article{height:150px}</style>
                <img src="/decor"><div id="pane"><main></main></div>
                <button id="next" onclick="load(2)">next</button><a id="link" href="/?page=2">next</a>
                <span id="end" style="display:none">end</span><script>
                let loaded=false;
                async function load(n){
                  let rows=await (await fetch('/api?page='+n)).json();
                  document.querySelector('main').insertAdjacentHTML('beforeend', rows.map(r=>
                    `<article data-id="${r.id}">${r.images.map(u=>`<img src="${u}">`).join('')}
                     <a class="file" href="${r.file}">file</a></article>`).join(''));
                  if(n===2){document.querySelector('#next').disabled=true;
                    document.querySelector('#link').remove();document.querySelector('#end').style.display='block'}
                }
                document.querySelector('#pane').onscroll=()=>{if(!loaded){loaded=true;load(2)}};
                load(INITIAL);
                </script>'''.replace('INITIAL', n).encode()
                kind = 'text/html'
            elif path == '/api':
                n = int(query['page'][0])
                rows = [dict(id=str(n), images=[f'/image{n}?signature=private-{n}', f'/other{n}'],
                             file=f'/file{n}')]
                body, kind = json.dumps(rows).encode(), 'application/json'
            elif path == '/image1':
                if (self.headers.get('Sec-Fetch-Dest') != 'image'
                        or 'probe=ok' not in self.headers.get('Cookie', '')):
                    status = 403
                elif hits[('GET', path)] > 1:
                    status = 410
            elif path.startswith('/file'):
                body, kind = b'attachment', 'application/octet-stream'
                if 'probe=ok' not in self.headers.get('Cookie', ''):
                    status = 403
            elif path == '/missing':
                status = 404
            elif path == '/large':
                body = b'x' * 2048
            elif path == '/redirect':
                status = 302
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            if path == '/redirect':
                self.send_header('Location', '/other1')
            if path == '/':
                self.send_header('Set-Cookie', 'probe=ok; Path=/; HttpOnly')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/', hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def rule(url, source='dom', pagination='click'):
    pagination_rule = dict(type=pagination, next_selector='#link' if pagination == 'link' else '#next')
    if pagination == 'scroll':
        pagination_rule = dict(type='scroll', container_selector='#pane', end_selector='#end', max_scrolls=5)
    data = dict(start_url=url, max_items=2, timeout_ms=3000, content_timeout_ms=2000,
                resources=[dict(field='images', mode='response'), dict(field='file', mode='request')],
                dedupe_by=['id'])
    if source == 'dom':
        data.update(item_selector='article', fields=dict(
            id=dict(selector=':scope', extract='attribute', attribute='data-id'),
            images=dict(selector='img', extract='attributes', attribute='src', transform='absolute_url'),
            file=dict(selector='a.file', extract='attribute', attribute='href', transform='absolute_url'),
        ), pagination=pagination_rule)
    else:
        data.update(version='3', network=dict(url_pattern=url+'api', records_path=[],
            fields={name: dict(path=[name]) for name in ('id', 'images', 'file')}, pagination=pagination_rule))
    return ScraperSpec.model_validate(data)


# 同一规则默认只提取 URL，启用后两类数据源共享保存器，不重放原图片请求。
@pytest.mark.parametrize('source', ['dom', 'network'])
@pytest.mark.parametrize('pagination', ['click', 'scroll', 'link'])
@pytest.mark.parametrize('enabled', [False, True])
def test_resources_across_batches(site, tmp_path, source, pagination, enabled):
    url, hits = site
    options = ResourceOptions(tmp_path, timeout_ms=2000) if enabled else None
    result = ScraperExecutor(resources=options).run(rule(url, source, pagination))
    assert [r['id'] for r in result.records] == ['1', '2']
    assert hits[('GET', '/image1')] == 1
    assert hits[('GET', '/file1')] == int(enabled)
    assert hits[('GET', '/file2')] == int(enabled)
    if not enabled:
        assert result.resources == [] and result.resource_directory is None
        assert list(tmp_path.iterdir()) == []
        return
    assert result.resource_errors == 0
    assert len(result.resources) == 6
    root = Path(result.resource_directory)
    assert json.loads((root / 'manifest.json').read_text()) == result.resources
    assert len({entry['path'] for entry in result.resources}) == 6
    for entry in result.resources:
        assert entry['record'] in {1, 2}
        assert entry['position'] in ({1, 2} if entry['field'] == 'images' else {1})
        assert (root / entry['path']).read_bytes() == (PNG if entry['field'] == 'images' else b'attachment')
    assert 'private-' not in (root / 'manifest.json').read_text()
    assert len(list(root.iterdir())) == 7


# 单条失败不丢记录，不自动切换模式；大小/数量/总量限制均通过同一保存路径执行。
@pytest.mark.parametrize('target,mode,updates,status', [
    ('/missing', 'request', {}, 'http_error'),
    ('/large', 'request', {'max_file_bytes': 100}, 'size_limit'),
    ('/large', 'request', {'max_total_bytes': 100}, 'size_limit'),
    ('blob:unavailable', 'request', {}, 'unsupported_url'),
    ('/never-loaded', 'response', {}, 'response_timeout'),
    ('/image1', 'request', {}, 'http_error'),
])
def test_resource_failures_keep_records(site, tmp_path, target, mode, updates, status):
    url, _ = site
    spec = ScraperSpec.model_validate(dict(start_url=url, item_selector='article',
        fields={'asset': dict(selector='a', extract='attribute', attribute='href')},
        resources=[dict(field='asset', mode=mode)], max_items=1))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route(url, lambda route: route.fulfill(content_type='text/html',
            body=f'<article><a href="{target}">asset</a></article>'))
        result = ScraperExecutor(resources=ResourceOptions(tmp_path, timeout_ms=100, **updates)).run_page(spec, page)
        browser.close()
    assert len(result.records) == 1 and not result.errors
    assert result.resources[0]['status'] == status
    assert result.resource_errors == 1


def test_resource_protocol_rejects_unbound_and_duplicate_fields(site):
    spec = rule(site[0]).model_dump()
    for resources in ([dict(field='unknown', mode='request')], [spec['resources'][0]] * 2):
        with pytest.raises(ValidationError):
            ScraperSpec.model_validate({**spec, 'resources': resources})


@pytest.mark.parametrize('updates,status', [({'max_files': 1}, 'file_limit'),
                                           ({'max_per_record': 1}, 'record_limit')])
def test_resource_count_limits(site, tmp_path, updates, status):
    spec = rule(site[0]).model_copy(update={'max_items': 1})
    result = ScraperExecutor(resources=ResourceOptions(tmp_path, **updates)).run(spec)
    assert result.resources[0]['status'] == 'saved'
    assert any(entry['status'] == status for entry in result.resources)


# 真正注册的 Agent 工具只读元数据/HEAD，完整正文 API 即使不可用也不影响证据验证。
@pytest.mark.parametrize('source', ['dom', 'network'])
def test_agent_resource_evidence_never_reads_bodies_or_writes_files(site, tmp_path, monkeypatch, source):
    import asyncio
    from types import SimpleNamespace

    pytest.importorskip('browser_use')
    from playwright.async_api import async_playwright, Response, APIResponse
    from autoscraper.agent.tools import build_tools
    from autoscraper.agent.resources import resource_key
    from autoscraper.browser.resources import ResourceEvidence
    from autoscraper.browser.network import CapturedResponse

    async def forbidden(*args, **kwargs):
        raise AssertionError('Agent must not read resource bodies')

    monkeypatch.setattr(Response, 'body', forbidden)
    monkeypatch.setattr(APIResponse, 'body', forbidden)
    url, hits = site

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context()
            evidence = ResourceEvidence(context)
            page = await context.new_page()
            await page.goto(url, wait_until='networkidle')
            capture = SimpleNamespace(resources=evidence, buffer=SimpleNamespace(entries={}))
            if source == 'network':
                capture.buffer.entries[1] = CapturedResponse(1, url+'api?page=1', 'GET',
                    page_url=url, complete=True, body=[dict(id='1',
                        images=['/image1?signature=private-1', '/other1'], file='/file1')])
            tools = build_tools(network_capture=capture, check_submission=True, listing_url=url)

            async def current_url():
                return page.url

            action = tools.registry.registry.actions['test_resources'].function
            spec = rule(url, source)
            output = await action(spec=spec, browser_session=SimpleNamespace(get_current_page_url=current_url))
            data = json.loads(output.extracted_content)
            assert all(result['passed'] for result in data['results'])
            assert 'private-' not in output.extracted_content
            assert all(resource_key(spec, r) in tools.submission.resource_evidence for r in spec.resources)
            changed = spec.resources[0].model_copy(update={'mode': 'request'})
            assert resource_key(spec, changed) not in tools.submission.resource_evidence
            # 重复调用不会无限探测，也不会把旧的成功证据用于探测失败后的提交。
            for _ in range(6):
                await action(spec=spec, browser_session=SimpleNamespace(get_current_page_url=current_url))
            assert evidence.probes == 6
            assert resource_key(spec, spec.resources[1]) not in tools.submission.resource_evidence
            evidence.close()
            await browser.close()

    asyncio.run(run())
    assert hits[('GET', '/file1')] == 0 and hits[('HEAD', '/file1')] == 6
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('interface', ['cli', 'ui'])
def test_standalone_resource_outputs(site, tmp_path, monkeypatch, interface):
    from autoscraper.browser.access import BrowserAccess
    from autoscraper.cli.scraper import main
    from autoscraper.ui.runtime import Job, JobManager, RunConfig
    import zipfile
    import sys

    spec = rule(site[0])
    if interface == 'cli':
        path = tmp_path / 'rule.json'
        path.write_text(spec.model_dump_json())
        monkeypatch.setattr(sys, 'argv', ['autoscraper', str(path), '--output', str(tmp_path / 'data.csv'),
                                        '--download-resources', str(tmp_path / 'assets')])
        assert main() == 0
        manifests = list((tmp_path / 'assets').glob('*/manifest.json'))
        assert len(manifests) == 1 and len(json.loads(manifests[0].read_text())) == 6
        assert 'images,file' in (tmp_path / 'data.csv').read_text().splitlines()[0]
    else:
        manager = JobManager(tmp_path / 'ui')
        output = tmp_path / 'ui' / 'artifacts' / 'run'
        output.mkdir()
        job = Job(RunConfig(mode='spec', spec_json=spec.model_dump_json(), download_resources=True))
        manager._execute(job, BrowserAccess(), output, None)
        assert job.summary['resource_errors'] == 0
        archive = next(path for path in job.files if path.endswith('.zip'))
        with zipfile.ZipFile(archive) as stream:
            assert len(json.loads(stream.read('manifest.json'))) == 6
            assert len(stream.namelist()) == 7


# 详情页在同一 Page 中逐条导航；同 URL 的旧响应不能污染下一条记录。
def test_detail_resources_are_saved_before_next_navigation(site, tmp_path):
    url, _ = site
    spec = ScraperSpec.model_validate(dict(version='2', start_url=url, item_selector='article',
        fields={'id': dict(selector=':scope', extract='attribute', attribute='data-id')},
        detail=dict(url_selector='a', fields={'image': dict(selector='img', extract='attribute', attribute='src')}),
        resources=[dict(field='image', mode='response')]))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context().new_page()
        page.context.route(url, lambda r: r.fulfill(content_type='text/html', body=
            '<article data-id="1"><a href="/detail/1">one</a></article>'
            '<article data-id="2"><a href="/detail/2">two</a></article>'))
        page.context.route('**/detail/*', lambda r: r.fulfill(content_type='text/html',
            body='<img src="/shared.png?variant=1">'))
        result = ScraperExecutor(resources=ResourceOptions(tmp_path)).run_page(spec, page)
        browser.close()
    assert [e['record'] for e in result.resources] == [1, 2]
    assert all(e['status'] == 'saved' for e in result.resources)
    assert all((Path(result.resource_directory) / e['path']).read_bytes() == PNG for e in result.resources)


def test_response_ambiguity_never_replays_or_guesses(site, tmp_path):
    url, hits = site
    spec = ScraperSpec.model_validate(dict(start_url=url, item_selector='article',
        fields={'image': dict(selector='img', extract='attribute', attribute='src')},
        resources=[dict(field='image', mode='response')]))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route(url, lambda r: r.fulfill(content_type='text/html', body='''
          <script>async function start(){
            await fetch('/same');await fetch('/same');
            document.body.innerHTML='<article><img data-url="/same"></article>';
            document.querySelector('img').setAttribute('src','/same');
          }window.onload=start;</script>'''))
        result = ScraperExecutor(resources=ResourceOptions(tmp_path)).run_page(spec, page)
        browser.close()
    assert result.resources[0]['status'] == 'ambiguous_response'
    assert hits[('GET', '/same')] <= 3


def test_response_redirect_keeps_original_record_url(site, tmp_path):
    url, hits = site
    spec = ScraperSpec.model_validate(dict(start_url=url, item_selector='article',
        fields={'image': dict(selector='img', extract='attribute', attribute='src')},
        resources=[dict(field='image', mode='response')]))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route(url, lambda r: r.fulfill(content_type='text/html',
            body='<article><img src="/redirect"></article>'))
        result = ScraperExecutor(resources=ResourceOptions(tmp_path)).run_page(spec, page)
        browser.close()
    assert result.records == [{'image': '/redirect'}]
    assert result.resources[0]['status'] == 'saved'
    assert hits[('GET', '/redirect')] == hits[('GET', '/other1')] == 1
