# 探索证据与提交检查：只认可工具在对应页面返回的原样选择器测试，不采信模型记忆。
# 本轮证据留在内存中；执行器仍独立验证完整数据，采样通过不代表语义或全量正确。
"""Small per-exploration evidence store and deterministic submission checks."""

from urllib.parse import urldefrag, urljoin

from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.validation import Feedback, validate_spec


# URL 保留查询参数，只忽略 fragment；导航重定向通过本轮别名显式关联。
def page_key(url):
    return urldefrag(str(url)).url


# 记录每个 URL/记录根/字段/属性的最近一次测试，以及动作前后的增长证据。
class SubmissionChecks:
    def __init__(self, requirements=None):
        self.requirements = requirements
        self.tests = {}
        self.aliases = {}
        self.pending = None
        self.advances = set()
        self.last_issues = []

    # 动作开始前保存测试结果，后续测试只能与这份动作前证据比较。
    def begin_action(self, kind, url):
        self.pending = (kind, page_key(url), dict(self.tests))

    # 单字段与批量结果走同一条登记路径；失败覆盖旧成功，避免旧证据掩盖新失败。
    def remember(self, result, field_selector="", attribute=""):
        if not result.get("url") or "item_selector" not in result:
            return
        key = (page_key(result["url"]), result["item_selector"], field_selector, attribute or "")
        self.tests[key] = result
        if self.pending:
            kind, url, previous = self.pending
            old = previous.get(key)
            if url == key[0] and old and (result.get("count") or 0) > 0:
                if result["count"] > (old.get("count") or 0):
                    self.advances.add((url, key[1], kind, "append"))
                if result.get("samples") != old.get("samples"):
                    self.advances.add((url, key[1], kind, "replace"))

    # 数量等待可能开始时数据已增长，必须对比点击前测试数量，而非等待工具初始数量。
    def remember_wait(self, result):
        if not self.pending or result.get("status") != "ready" or result.get("count") is None:
            return
        kind, url, previous = self.pending
        if page_key(result.get("url", "")) != url:
            return
        selector = result.get("selector")
        counts = [r.get("count") or 0 for key, r in previous.items() if key[:2] == (url, selector)]
        if counts and result["count"] > max(counts):
            self.advances.add((url, selector, kind, "append"))

    # 导航实际 URL 与请求 URL 关联，支持正常重定向；不将任意详情页当作列表页证据。
    def actual_url(self, url):
        key = page_key(url)
        return self.aliases.get(key, key)

    # 使用最终规则的原样参数取证，规范化页面别名不改变选择器语义。
    def result(self, url, item_selector, selector, attribute=""):
        return self.tests.get((self.actual_url(url), item_selector, selector, attribute or ""))

    # 样本唯一性之外，必填字段还要求样本值非空；可选字段允许无匹配，但不允许歧义。
    @staticmethod
    def passed(result, required=True):
        if not result or not result.get("count") or not result.get("samples"):
            return False
        return all(
            sample.get("matches") == 1
            and (
                not required
                or any(
                    str(value).strip() for value in sample.get("values", []) if value is not None
                )
            )
            or (not required and sample.get("matches") == 0)
            for sample in result["samples"]
        )

    # 生成可直接回给 Agent 的修复指令；用户字段名以合并后的列表/详情字段为准。
    def check(self, spec: ScraperSpec):
        issues = validate_spec(spec, self.requirements) if self.requirements else []
        url = self.actual_url(spec.start_url)

        def require(test_url, item, selector, attribute="", required=True):
            result = self.result(test_url, item, selector, attribute)
            if not self.passed(result, required):
                issues.append(
                    Feedback(
                        code="selector_failure",
                        message=(
                            f"Test exact selector {selector!r} attribute={attribute!r} relative to "
                            f"{item!r} on {test_url}; it is untested or its latest sample failed. "
                            "Navigate to the correct listing/detail page and use test_fields/test_selector."
                        ),
                    )
                )
            return result

        for field in spec.fields.values():
            require(url, spec.item_selector, field.selector, field.attribute, field.required)
        if spec.detail:
            links = require(url, spec.item_selector, spec.detail.url_selector, "href")
            detail_urls = {
                self.actual_url(urljoin(url, value))
                for sample in (links or {}).get("samples", [])
                for value in sample.get("values", [])
                if value
            }
            # 详情字段必须在某个已观察到的详情链接目标上全部通过，不能混用不同页面证据。
            verified = any(
                all(
                    self.passed(
                        self.result(detail_url, "html", field.selector, field.attribute),
                        field.required,
                    )
                    for field in spec.detail.fields.values()
                )
                for detail_url in detail_urls
            )
            if not verified:
                issues.append(
                    Feedback(
                        code="selector_failure",
                        message=(
                            "Test every detail field with item_selector='html' on an observed detail URL. "
                            "Listing-page tests do not verify detail fields. Keep requested field names, "
                            "e.g. detail.fields.description; only avoid names already in listing fields."
                        ),
                    )
                )
        pagination = spec.pagination
        if pagination:
            if pagination.type in {"link", "click"}:
                control = require(
                    url,
                    pagination.next_selector,
                    "",
                    "href" if pagination.type == "link" else "",
                    pagination.type == "link",
                )
                if control and control.get("count") != 1:
                    issues.append(
                        Feedback(
                            code="selector_failure",
                            message="Pagination control must match exactly one element",
                        )
                    )
            if pagination.type in {"click", "scroll"}:
                mode = getattr(pagination, "mode", "append")
                if (url, spec.item_selector, pagination.type, mode) not in self.advances:
                    issues.append(
                        Feedback(
                            code="selector_failure",
                            message=(
                                f"Verify {pagination.type}/{mode} record progression on the listing. "
                                "For append, click/scroll then wait_for_selector(min_count=N+1) or retest "
                                "the same item selector; for replace, retest fields after clicking."
                            ),
                        )
                    )
        return issues
