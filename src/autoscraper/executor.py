"""Deterministic CSS-only execution. Never evaluates rule-provided code."""

from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

from .spec import ScraperSpec


class ScrapeError(RuntimeError):
    """Fatal navigation, selector, pagination or extraction error."""


class ItemError(ScrapeError):
    """A required value is missing from an individual item."""


@dataclass
class ScrapeResult:
    records: list[dict[str, str | None]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_visited: int = 0
    duplicates: int = 0
    stop_reason: str = ""


def http_url(base: str, value: str) -> str:
    url = urljoin(base, value)
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ItemError(f"Unsupported URL scheme: {url!r}")
    return url


def css(root: Page | Locator, selector: str) -> Locator:
    # Do not allow Playwright selector chains to switch away from the CSS engine.
    if ">>" in selector:
        raise ScrapeError("Selector chains are not supported; use a CSS selector")
    return root.locator(f"css={selector}")


def extract_item(item: Locator, spec: ScraperSpec, base_url: str) -> dict[str, str | None]:
    record = {}
    for name, rule in spec.fields.items():
        matches = css(item, rule.selector)
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


class ScraperExecutor:
    def run(self, spec: ScraperSpec) -> ScrapeResult:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                page = browser.new_page()
                return self.run_page(spec, page)
            finally:
                browser.close()

    def run_page(self, spec: ScraperSpec, page: Page) -> ScrapeResult:
        """Accept an owned page for fixture routing or embedding; caller closes it."""
        result = ScrapeResult()
        seen_records: set[tuple] = set()
        seen_pages: set[str] = set()
        page.set_default_timeout(spec.timeout_ms)
        next_url = str(spec.start_url)
        try:
            while result.pages_visited < spec.max_pages:
                identity = urldefrag(next_url).url
                if identity in seen_pages:
                    result.stop_reason = "pagination_cycle"
                    break
                response = page.goto(next_url, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    raise ScrapeError(f"HTTP {response.status} at {page.url}")
                final_identity = urldefrag(page.url).url
                if final_identity in seen_pages:
                    result.stop_reason = "pagination_cycle"
                    break
                seen_pages.update((identity, final_identity))
                result.pages_visited += 1
                items = css(page, spec.item_selector)
                items.first.wait_for(state="attached")
                for index in range(items.count()):
                    try:
                        record = extract_item(items.nth(index), spec, page.url)
                    except ItemError as exc:
                        message = f"{page.url} item {index + 1}: {exc}"
                        if spec.on_item_error == "fail":
                            raise ScrapeError(message) from exc
                        result.errors.append(message)
                        continue
                    keys = spec.dedupe_by or list(spec.fields)
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
        return result
