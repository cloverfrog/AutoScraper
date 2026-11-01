# 资源验证按候选的精确字段规则关联记录，只向模型返回有界元数据。
# HEAD 探测只提供可获取证据，不承诺后续 GET 或完整下载必然成功。
"""Lightweight resource evidence, never bulk acquisition or file output."""

import json

from browser_use import ActionResult
from browser_use.browser.session import BrowserSession

from autoscraper.scraping.network import extract_json_record, record_items, matches
from autoscraper.scraping.resources import resource_url
from autoscraper.scraping.spec import ScraperSpec
from .submission import resource_key


def register_resource_tools(tools, capture):
    evidence = capture.resources

    @tools.action(description="Verify resource rules against at most 3 current records per field. "
                  "response mode uses observed GET status/type only; request mode uses at most "
                  "6 HEAD probes per exploration (no bodies, no files). Does not guarantee a later "
                  "full GET succeeds. Test exact candidate resource rules before done. "
                  "On detail pages test detail resource fields; on listing pages test listing fields.")
    async def test_resources(spec: ScraperSpec, browser_session: BrowserSession) -> ActionResult:
        spec = ScraperSpec.model_validate(spec)
        current_url = await browser_session.get_current_page_url()
        page = next((p for p in evidence.context.pages if p.url == current_url), None)
        # 当前页由工具分发注入，避免新建页面或触发额外导航。
        if page is None:
            return ActionResult(extracted_content=json.dumps(dict(status="no_current_page")))
        results = []
        for rule in spec.resources:
            if not spec.network:
                listing_url = tools.submission.actual_url(spec.start_url) if tools.submission else str(spec.start_url)
                detail = bool(spec.detail and rule.field in spec.detail.fields)
                if detail == (page.url == listing_url):
                    continue
            key = resource_key(spec, rule)
            if tools.submission:
                tools.submission.resource_evidence.discard(key)
            samples = []
            try:
                if spec.network:
                    for entry in capture.buffer.entries.values():
                        if entry.page_url == page.url and entry.complete and not entry.error and matches(spec.network, entry.url, entry.method):
                            for item in record_items(entry.body, spec.network):
                                samples.append(extract_json_record(item, spec.network).get(rule.field))
                                if len(samples) >= 3:
                                    break
                        if len(samples) >= 3:
                            break
                else:
                    field = spec.all_fields[rule.field]
                    detail = bool(spec.detail and rule.field in spec.detail.fields)
                    root = "html" if detail else spec.item_selector
                    # 仅由应用固定脚本读取原始属性/文本，不使用会截断签名 URL 的显示样本。
                    samples = await page.locator(root).evaluate_all('''(items, f) =>
                        items.slice(0,3).map(item => {
                          const els = f.selector === ':scope' ? [item] : [...item.querySelectorAll(f.selector)];
                          const values = els.map(el => f.attribute ? el.getAttribute(f.attribute) : el.textContent);
                          return f.extract === 'attributes' ? values : values.length === 1 ? values[0] : null;
                        })''', field.model_dump())
                urls = []
                for sample in samples:
                    for value in sample if isinstance(sample, list) else [sample]:
                        url = resource_url(value, page.url)
                        if url and url not in urls:
                            urls.append(url)
                        if len(urls) >= 3:
                            break
                    if len(urls) >= 3:
                        break
                checks = []
                for url in urls:
                    if rule.mode == "response":
                        observed = [entry for entry in evidence.entries if url in entry["urls"]
                                    and entry["page"] == page.url]
                        checks.append(dict(status="observed" if any(
                            200 <= entry["status"] < 300 and entry["content_type"] for entry in observed
                        ) else "not_observed"))
                    elif evidence.probes >= 6:
                        checks.append(dict(status="probe_limit"))
                    else:
                        evidence.probes += 1
                        response = await evidence.context.request.head(url, timeout=3000)
                        try:
                            checks.append(dict(status="head_ok" if 200 <= response.status < 300
                                               else "head_failed", http_status=response.status))
                        finally:
                            await response.dispose()
                passed = bool(checks) and all(c["status"] in {"observed", "head_ok"} for c in checks)
                if passed and tools.submission:
                    tools.submission.resource_evidence.add(key)
                results.append(dict(field=rule.field, mode=rule.mode, passed=passed, checks=checks))
            except Exception:
                results.append(dict(field=rule.field, mode=rule.mode, passed=False, status="probe_failed"))
        return ActionResult(extracted_content=json.dumps(dict(results=results)))
