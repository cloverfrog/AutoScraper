# 探索证据与提交检查：只认可工具在对应页面返回的原样选择器测试，不采信模型记忆。
# 本轮证据留在内存中；执行器仍独立验证完整数据，采样通过不代表语义或全量正确。
"""Small per-exploration evidence store and deterministic submission checks."""

from urllib.parse import urldefrag, urljoin

from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.validation import Feedback, validate_spec


# URL 保留查询参数，只忽略 fragment；导航重定向通过本轮别名显式关联。
def page_key(url):
    return urldefrag(str(url)).url


# 字段测试与记录计数分别保存：计数不依赖字段参数，替换模式仍比较同一字段的值。
class SubmissionChecks:
    def __init__(self, requirements=None):
        self.requirements = requirements
        self.tests = {}
        self.counts = {}
        self.links = {}
        self.aliases = {}
        self.pending = None
        self.advances = set()
        self.last_issues = []

    # 动作前固定字段和数量快照；后续工具结果不能覆盖用于比较的基线。
    def begin_action(self, kind, url):
        self.pending = (kind, page_key(url), dict(self.tests), dict(self.counts))

    # 数量来自真实 DOM，可由字段测试、单独计数或数量等待提供，不要求字段参数一致。
    def remember_count(self, url, item_selector, count):
        if not url or not item_selector or not isinstance(count, int) or count < 0:
            return
        key = (page_key(url), item_selector)
        self.counts[key] = count
        if self.pending:
            kind, action_url, _, previous = self.pending
            before = previous.get(key)
            if action_url == key[0] and before is not None and count > before:
                self.advances.add((key[0], item_selector, kind, "append"))

    # 单字段与批量结果走同一条登记路径；失败覆盖旧成功，避免旧证据掩盖新失败。
    def remember(self, result, field_selector="", attribute=""):
        if not result.get("url") or "item_selector" not in result:
            return
        key = (page_key(result["url"]), result["item_selector"], field_selector, attribute or "")
        self.tests[key] = result
        self.remember_count(result["url"], result["item_selector"], result.get("count"))
        # 保留曾测试通过的详情目标，替换列表后复核新链接不会抹掉先前详情页的有效证据。
        if attribute == "href" and self.passed(result):
            self.links.setdefault(key, set()).update(
                urljoin(result["url"], value)
                for sample in result["samples"]
                for value in sample["values"]
                if value
            )
        if self.pending:
            kind, url, previous, _ = self.pending
            old = previous.get(key)
            if url == key[0] and self.passed(old) and self.passed(result):
                if result.get("samples") != old.get("samples"):
                    self.advances.add((url, key[1], kind, "replace"))

    # 数量等待可能开始时数据已增长，必须对比点击前测试数量，而非等待工具初始数量。
    def remember_wait(self, result):
        if result.get("status") not in {"ready", "timeout"}:
            return
        self.remember_count(result.get("url"), result.get("selector"), result.get("count"))

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
            require(url, spec.item_selector, spec.detail.url_selector, "href")
            detail_urls = {
                self.actual_url(value)
                for value in self.links.get(
                    (url, spec.item_selector, spec.detail.url_selector, "href"), ()
                )
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
