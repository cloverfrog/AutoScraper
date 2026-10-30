# 导航回归：错误文档的客户端跳转可恢复，真实 HTTP/网络错误和原超时约束仍需保留。
"""Error documents may redirect to a healthy SPA; real HTTP failures stay failures."""

import time

import pytest
from playwright.sync_api import sync_playwright

from autoscraper.scraping.executor import (
    NavigationError,
    NavigationTimeout,
    ScraperExecutor,
    navigate,
)
from autoscraper.scraping.spec import ScraperSpec


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser.new_page()
        browser.close()


def route_documents(page, documents):
    def route(request):
        status, body = documents[request.request.url]
        request.fulfill(status=status, content_type="text/html", body=body)

    page.route("**/*", route)


@pytest.mark.parametrize("delay", [0, 150])
def test_error_page_redirects_to_spa_and_restores_original_url(page, delay):
    url = "https://fixture.test/inventory.html"
    route_documents(
        page,
        {
            url: (
                404,
                f"<script>setTimeout(() => location.replace('/?route=inventory'), {delay})</script>",
            ),
            "https://fixture.test/?route=inventory": (
                200,
                "<script>history.replaceState(null, '', '/inventory.html')</script>"
                "<article><h2>Recovered</h2></article>",
            ),
        },
    )
    spec = ScraperSpec(
        start_url=url,
        item_selector="article",
        fields={"title": {"selector": "h2"}},
        max_items=1,
        timeout_ms=3000,
    )
    result = ScraperExecutor().run_page(spec, page)
    assert result.records == [{"title": "Recovered"}]
    assert page.url == url  # SPA 恢复了原地址，仅比较 URL 是否变化无法判断跳转是否成功。


def test_multiple_error_redirects_share_navigation_deadline(page):
    route_documents(
        page,
        {
            "https://fixture.test/start": (404, "<script>location.replace('/middle')</script>"),
            "https://fixture.test/middle": (503, "<script>location.replace('/ready')</script>"),
            "https://fixture.test/ready": (200, "<h1>Ready</h1>"),
        },
    )
    response = navigate(page, "https://fixture.test/start", 3000)
    assert response.status == 200
    assert page.locator("h1").inner_text() == "Ready"


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_error_without_redirect_still_fails_within_budget(page, status):
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=status, content_type="text/html", body="<article>Not valid data</article>"
        ),
    )
    started = time.monotonic()
    with pytest.raises(NavigationError, match=f"HTTP {status}"):
        navigate(page, "https://fixture.test/error", 350)
    assert time.monotonic() - started < 2


def test_successful_iframe_cannot_mask_failed_main_document(page):
    route_documents(
        page,
        {
            "https://fixture.test/error": (404, "<iframe src='/frame'></iframe>"),
            "https://fixture.test/frame": (200, "<h1>Subframe success</h1>"),
        },
    )
    with pytest.raises(NavigationError, match="HTTP 404"):
        navigate(page, "https://fixture.test/error", 500)


def test_redirect_destination_http_error_is_preserved(page):
    route_documents(
        page,
        {
            "https://fixture.test/start": (404, "<script>location.replace('/denied')</script>"),
            "https://fixture.test/denied": (403, "<h1>Access denied</h1>"),
        },
    )
    with pytest.raises(NavigationError, match="HTTP 403"):
        navigate(page, "https://fixture.test/start", 500)


def test_redirect_network_failure_stays_navigation_failure(page):
    def route(request):
        if request.request.url.endswith("/start"):
            request.fulfill(
                status=404,
                content_type="text/html",
                body="<script>location.replace('/offline')</script>",
            )
        else:
            request.abort("connectionfailed")

    page.route("**/*", route)
    with pytest.raises(NavigationError, match="ERR_CONNECTION_FAILED"):
        navigate(page, "https://fixture.test/start", 1500)


def test_redirect_loading_uses_remaining_original_timeout(page):
    def route(request):
        if request.request.url.endswith("/start"):
            request.fulfill(
                status=404,
                content_type="text/html",
                body="<script>location.replace('/slow')</script>",
            )
        # /slow 故意不返回响应，跳转后的加载也必须受原导航截止时间约束。

    page.route("**/*", route)
    with pytest.raises(NavigationTimeout):
        navigate(page, "https://fixture.test/start", 500)
