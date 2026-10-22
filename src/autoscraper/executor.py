"""Deterministic CSS-only execution. Never evaluates rule-provided code."""

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urldefrag, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeout, sync_playwright
from pydantic import HttpUrl, TypeAdapter

from .spec import FieldSpec, ScraperSpec


class ScrapeError(RuntimeError):
    """Fatal navigation, selector, pagination or extraction error."""


class ItemError(ScrapeError):
    """An individual item could not be extracted."""


class NavigationError(ItemError):
    """A navigation failed, without evidence that extraction selectors are wrong."""

    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(f"Navigation failed at {url}: {message}")


@dataclass
class ScrapeFailure:
    """Typed diagnostic accompanying a legacy string in ScrapeResult.errors."""

    code: Literal["navigation_failure", "selector_failure"]
    message: str
    url: str


@dataclass
class ScrapeResult:
    records: list[dict[str, str | None]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_visited: int = 0
    detail_pages_visited: int = 0
    duplicates: int = 0
    stop_reason: str = ""
    error_details: list[ScrapeFailure] = field(default_factory=list)


def navigate(page: Page, url: str, timeout_ms: int):
    """Classify errors at the operation boundary, not by matching error text."""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PlaywrightError as exc:
        raise NavigationError(url, str(exc)) from exc
    if response is not None and response.status >= 400:
        raise NavigationError(url, f"HTTP {response.status} at {page.url}")
    return response


def http_url(base: str, value: str) -> str:
    try:
        if urlsplit(value).scheme in {"http", "https"}:
            TypeAdapter(HttpUrl).validate_python(value)
        url = urljoin(base, value)
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ItemError(f"Unsupported URL scheme: {url!r}")
        TypeAdapter(HttpUrl).validate_python(url)
    except ValueError as exc:
        raise ItemError(f"Invalid HTTP(S) URL: {value!r}") from exc
    return url


def css(root: Page | Locator, selector: str) -> Locator:
    # Do not allow Playwright selector chains to switch away from the CSS engine.
    if ">>" in selector:
        raise ScrapeError("Selector chains are not supported; use a CSS selector")
    return root.locator(f"css={selector}")


def extract_fields(
    item: Page | Locator, fields: dict[str, FieldSpec], base_url: str, *, wait: bool = False
) -> dict[str, str | None]:
    record = {}
    for name, rule in fields.items():
        matches = css(item, rule.selector)
        count = matches.count()  # Invalid CSS is a fatal rule error, even for optional fields.
        if wait and rule.required and count == 0:
            try:
                matches.first.wait_for(state="attached")
            except PlaywrightTimeout as exc:
                raise ItemError(f"Required field {name!r} is missing or timed out") from exc
        if matches.count() > 1:
            raise ItemError(f"Field {name!r} matched multiple elements")
        value = None
        if matches.count():
            value = (
                matches.inner_text()
                if rule.extract == "text"
                else matches.get_attribute(rule.attribute)
            )
            if value is not None:
                value = " ".join(value.split())
        if not value:
            if rule.required:
                raise ItemError(f"Required field {name!r} is missing or empty")
            value = None
        if value is not None and rule.transform == "absolute_url":
            value = http_url(base_url, value)
        record[name] = value
    return record


def extract_item(item: Locator, spec: ScraperSpec, base_url: str) -> dict[str, str | None]:
    return extract_fields(item, spec.fields, base_url)


def extract_detail(page: Page, url: str, spec: ScraperSpec) -> dict[str, str | None]:
    navigate(page, url, spec.timeout_ms)
    http_url(page.url, page.url)
    try:
        return extract_fields(page, spec.detail.fields, page.url, wait=True)
    except PlaywrightTimeout as exc:
        raise ItemError(f"Detail field extraction timed out: {exc}") from exc


class ScraperExecutor:
    def run(self, spec: ScraperSpec) -> ScrapeResult:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context = browser.new_context()
                page = context.new_page()
                return self.run_page(spec, page)
            finally:
                browser.close()

    def run_page(self, spec: ScraperSpec, page: Page) -> ScrapeResult:
        """Accept a caller-owned page; detail rules need an explicit BrowserContext."""
        result = ScrapeResult()
        seen_records: set[tuple] = set()
        seen_pages: set[str] = set()
        page.set_default_timeout(spec.timeout_ms)
        next_url = str(spec.start_url)
        detail_page = None
        try:
            if spec.detail:
                detail_page = page.context.new_page()
                detail_page.set_default_timeout(spec.timeout_ms)

            while result.pages_visited < spec.max_pages:
                identity = urldefrag(next_url).url
                if identity in seen_pages:
                    result.stop_reason = "pagination_cycle"
                    break
                navigate(page, next_url, spec.timeout_ms)
                final_identity = urldefrag(page.url).url
                if final_identity in seen_pages:
                    result.stop_reason = "pagination_cycle"
                    break
                seen_pages.update((identity, final_identity))
                result.pages_visited += 1
                items = css(page, spec.item_selector)
                items.first.wait_for(state="attached")
                for index in range(items.count()):
                    detail_url = None
                    try:
                        item = items.nth(index)
                        record = extract_item(item, spec, page.url)
                        keys = spec.dedupe_by or list(spec.all_fields)
                        if all(key in record for key in keys):
                            if tuple(record[key] for key in keys) in seen_records:
                                result.duplicates += 1
                                continue
                        if spec.detail:
                            link = css(item, spec.detail.url_selector)
                            if link.count() != 1:
                                raise ItemError("Detail link must match exactly one element")
                            href = link.get_attribute("href")
                            if not href or not href.strip():
                                raise ItemError("Detail link requires a nonempty href")
                            detail_url = http_url(page.url, href.strip())
                            if result.detail_pages_visited >= spec.detail.max_pages:
                                result.stop_reason = "detail_limit"
                                return result
                            result.detail_pages_visited += 1
                            record.update(extract_detail(detail_page, detail_url, spec))
                    except ItemError as exc:
                        message = f"{page.url} item {index + 1} detail={detail_url!r}: {exc}"
                        if spec.on_item_error == "fail":
                            if isinstance(exc, NavigationError):
                                raise NavigationError(exc.url, message) from exc
                            raise ScrapeError(message) from exc
                        result.errors.append(message)
                        result.error_details.append(
                            ScrapeFailure(
                                code=(
                                    "navigation_failure"
                                    if isinstance(exc, NavigationError)
                                    else "selector_failure"
                                ),
                                message=message,
                                url=exc.url if isinstance(exc, NavigationError) else page.url,
                            )
                        )
                        continue
                    keys = spec.dedupe_by or list(spec.all_fields)
                    identity_record = tuple(record[key] for key in keys)
                    if identity_record in seen_records:
                        result.duplicates += 1
                        continue
                    seen_records.add(identity_record)
                    result.records.append(record)
                    if len(result.records) >= spec.max_items:
                        result.stop_reason = "max_items"
                        return result
                if spec.pagination is None:
                    result.stop_reason = "no_pagination"
                    break
                button = css(page, spec.pagination.next_selector)
                if button.count() == 0:
                    result.stop_reason = "no_next"
                    break
                if button.count() != 1:
                    raise ScrapeError("Next selector must match exactly one element")
                if button.is_disabled() or button.get_attribute("aria-disabled") == "true":
                    result.stop_reason = "no_next"
                    break
                href = button.get_attribute("href")
                if not href:
                    raise ScrapeError("Phase 1 pagination requires a Next link with href")
                next_url = http_url(page.url, href)
            else:
                result.stop_reason = "max_pages"
        except PlaywrightError as exc:
            raise ScrapeError(f"Browser failure at {page.url}: {exc}") from exc
        finally:
            if detail_page is not None:
                detail_page.close()
        return result
