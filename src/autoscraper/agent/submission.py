# 探索证据与提交检查：只认可工具在对应页面返回的原样选择器测试，不采信模型记忆。
# 本轮证据留在内存中；执行器仍独立验证完整数据，采样通过不代表语义或全量正确。
"""Small per-exploration evidence store and deterministic submission checks."""

from urllib.parse import urldefrag, urljoin

from autoscraper.scraping.spec import ScraperSpec
from autoscraper.scraping.values import has_field_value
from .errors import InvalidSpecError
from autoscraper.workflow.validation import Feedback, validate_spec


# URL 保留查询参数，只忽略 fragment；导航重定向通过本轮别名显式关联。
def page_key(url):
    return urldefrag(str(url)).url


# 字段测试按提取方式隔离；计数不依赖字段参数，替换模式仍比较同一规则的值。
class SubmissionChecks:
    def __init__(self, requirements=None, *, listing_url=None, reference_spec=None):
        self.requirements = requirements
        self.listing_url = listing_url
        self.reference_spec = reference_spec
        self.tests = {}
        self.counts = {}
        self.links = {}
        self.aliases = {}
        self.pending = None
        self.advances = set()
        self.last_issues = []
        self.last_candidate = None
        self.network_summary = None

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
        mode = result.get("extract", "attribute" if attribute else "text")
        key = (page_key(result["url"]), result["item_selector"], field_selector, attribute or "", mode)
        self.tests[key] = result
        self.remember_count(result["url"], result["item_selector"], result.get("count"))
        # 保留曾测试通过的详情目标，替换列表后复核新链接不会抹掉先前详情页的有效证据。
        if attribute == "href" and self.passed(result, nonempty=True):
            self.links.setdefault(key[:4], set()).update(
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
    def result(self, url, item_selector, selector, attribute="", extract=None):
        mode = extract or ("attribute" if attribute else "text")
        return self.tests.get((self.actual_url(url), item_selector, selector, attribute or "", mode))

    # 样本检查定位与歧义，不把真实空值当失败；导航链接另行要求非空。
    @staticmethod
    def passed(result, required=True, extract=None, *, nonempty=False):
        if not result or not result.get("count") or not result.get("samples"):
            return False
        mode = result.get("extract", "attribute" if result.get("attribute") else "text")
        if extract is not None and extract != mode:
            return False
        # 多元素证据只用于对应提取方式，不能冒充单值选择器唯一性证据。
        if mode == "count":
            return all(sample.get("values") == [sample.get("matches")]
                       and type(sample.get("matches")) is int and sample["matches"] >= 0
                       for sample in result["samples"])
        if mode == "texts":
            return all(isinstance(sample.get("values"), list)
                       and all(isinstance(value, str) for value in sample["values"])
                       for sample in result["samples"])
        if any(sample.get("matches") not in {0, 1} for sample in result["samples"]):
            return False
        values = [value for sample in result["samples"] for value in sample.get("values", [])]
        if nonempty:
            return all(sample.get("matches") == 1 for sample in result["samples"]) and any(
                has_field_value(value) for value in values
            )
        return not required or result.get("resolved_count", sum(value is not None for value in values)) > 0

    # 用户要求的字段需要定位证据，但这不等于每条记录都必须有非空值。
    def needs_evidence(self, name, field):
        return field.required or bool(self.requirements and name in self.requirements.fields)

    # 页内待办只依据本会话的实际测试生成；参考候选用于提示，不导入历史取证或阻止导航。
    def page_work(self, url):
        page = self.actual_url(url)
        spec = self.last_candidate or self.reference_spec
        listing = spec.start_url if spec is not None else self.listing_url
        is_listing = listing is not None and page == self.actual_url(listing)
        detail_urls = set()
        if spec is not None and spec.detail:
            detail_urls = {
                self.actual_url(value) for value in self.links.get(
                    (self.actual_url(spec.start_url), spec.item_selector, spec.detail.url_selector, "href"), ()
                )
            }
        role = "listing" if is_listing else "detail" if page in detail_urls else "other"
        if spec is not None and spec.network is not None:
            return {
                "url": page, "role": role, "candidate_pending": None,
                "guidance": "This DOM summary does not report network evidence. Use test_network "
                "with the exact source and dedupe_by; verify the initial page and pagination.",
            }
        field_tests = sum(1 for test_url, _, field, _, _ in self.tests if test_url == page and field)
        pending = []
        if spec is not None and spec.network is None and role in {"listing", "detail"}:
            fields = spec.fields if is_listing else spec.detail.fields
            root = spec.item_selector if is_listing else "html"
            pending.extend(
                f"field:{name}" for name, field in fields.items()
                if not self.passed(
                    self.result(page, root, field.selector, field.attribute, field.extract),
                    self.needs_evidence(name, field), field.extract,
                )
            )
            if is_listing and spec.detail and not self.passed(
                self.result(page, root, spec.detail.url_selector, "href")
            ):
                pending.append("detail_link")
            pagination = spec.pagination
            if is_listing and pagination:
                if pagination.type in {"click", "link"}:
                    control = self.result(page, pagination.next_selector, "",
                                          "href" if pagination.type == "link" else "")
                    if not self.passed(control, pagination.type == "link", nonempty=pagination.type == "link") or control.get("count") != 1:
                        pending.append("pagination_control")
                if pagination.type in {"click", "scroll"}:
                    mode = getattr(pagination, "mode", "append")
                    if (page, root, pagination.type, mode) not in self.advances:
                        pending.append(f"pagination_progress:{pagination.type}/{mode}")
        guidance = (
            "Inspection alone is not a field test. Group locally available field/link and "
            "pagination checks before a detail visit where practical."
            if is_listing else
            "If this is a detail page, test its requested fields with item_selector='html' before leaving; "
            "inspecting their selectors does not verify them."
        )
        if spec is not None and role != "other" and not pending:
            guidance = (
                "Current-page checks for the supplied candidate are already covered. "
                "Avoid repeating unchanged tests; continue remaining work elsewhere or submit "
                "if all required evidence is recorded."
            )
        return {
            "url": page, "role": role, "field_tests": field_tests,
            "candidate_pending": pending[:12] if spec is not None and role != "other" else None,
            "omitted_pending": max(0, len(pending) - 12),
            "guidance": guidance,
        }

    # 修复只继承有界的选择器/URL/推进摘要，不复制原始页面文本或导入旧会话证据。
    def repair_summary(self):
        tests = list(self.tests.items())[-32:]
        summary = {
            "tests": [
                {"url": url, "item_selector": item, "field_selector": selector,
                 "attribute": attribute, "extract": result.get("extract"), "status": result.get("status"),
                 "count": result.get("count"), "passed": self.passed(result)}
                for (url, item, selector, attribute, _), result in tests
            ],
            "omitted_tests": max(0, len(self.tests) - len(tests)),
            "observed_link_urls": sorted({value for values in self.links.values() for value in values})[:8],
            "progression": [
                {"url": url, "item_selector": item, "type": kind, "mode": mode}
                for url, item, kind, mode in sorted(self.advances)[:8]
            ],
        }
        if self.network_summary is not None:
            summary["network_test"] = self.network_summary
        return summary

    # 拒绝原因与候选一起离开已结束的 Agent，避免工作流只能传回一段泛化错误。
    def rejection_error(self):
        return InvalidSpecError(
            "Submission rejected: "
            + "; ".join(issue.message for issue in self.last_issues)[:4000],
            candidate=self.last_candidate,
            evidence_summary=self.repair_summary(),
        )

    # 生成可直接回给 Agent 的修复指令；用户字段名以合并后的列表/详情字段为准。
    def check(self, spec: ScraperSpec):
        issues = validate_spec(spec, self.requirements) if self.requirements else []
        url = self.actual_url(spec.start_url)
        if spec.network is not None:
            pagination = spec.network.pagination
            if pagination is not None and pagination.type != "scroll":
                attribute = "href" if pagination.type == "link" else ""
                control = self.result(url, pagination.next_selector, "", attribute)
                if not self.passed(control, pagination.type == "link", nonempty=pagination.type == "link") or control.get("count") != 1:
                    issues.append(Feedback(code="selector_failure", message=
                        "Test the exact network pagination CSS control on the initial page "
                        "using test_selector (href for link pagination)."))
            return issues

        def require(test_url, item, selector, attribute="", required=True, extract=None, *, nonempty=False):
            result = self.result(test_url, item, selector, attribute, extract)
            if not self.passed(result, required, extract, nonempty=nonempty):
                if result is None:
                    reason = "No test exists for this exact page/root/field combination."
                else:
                    reason = (
                        f"The latest test failed: status={result.get('status')!r}, "
                        f"count={result.get('count')!r}."
                    )
                other_roots = sorted({
                    root for (page, root, field, attr, _), previous in self.tests.items()
                    if page == self.actual_url(test_url) and root != item
                    and field == selector and attr == (attribute or "")
                    and self.passed(previous, required, extract)
                })[:3]
                if other_roots:
                    reason += (
                        f" Passing evidence exists under different item roots {other_roots!r}; "
                        "it does not verify the current root. Check the root change before "
                        "rediscovering field selectors."
                    )
                issues.append(Feedback(
                    code="selector_failure",
                    message=f"Test exact selector {selector!r} attribute={attribute!r} "
                    f"extract={extract or ('attribute' if attribute else 'text')!r} relative to "
                    f"{item!r} on {test_url}. {reason} "
                    "Navigate to the correct listing/detail page and use test_fields/test_selector.",
                ))
            return result

        for name, field in spec.fields.items():
            require(url, spec.item_selector, field.selector, field.attribute, self.needs_evidence(name, field), field.extract)
        if spec.detail:
            require(url, spec.item_selector, spec.detail.url_selector, "href", nonempty=True)
            detail_urls = {
                self.actual_url(value)
                for value in self.links.get(
                    (url, spec.item_selector, spec.detail.url_selector, "href"), ()
                )
            }
            # 稀疏字段可在不同的已观察详情页定位，不强迫某一页包含全部字段。
            verified = bool(detail_urls) and all(
                any(self.passed(
                    self.result(detail_url, "html", field.selector, field.attribute, field.extract),
                    self.needs_evidence(name, field), field.extract,
                ) for detail_url in detail_urls)
                for name, field in spec.detail.fields.items()
            )
            if not verified:
                tested_urls = {
                    page for page, root, _, _, _ in self.tests if root == "html" and page != url
                }
                passed_urls = sorted(
                    page for page in tested_urls if all(
                        self.passed(
                            self.result(page, "html", field.selector, field.attribute, field.extract),
                            self.needs_evidence(name, field), field.extract,
                        )
                        for name, field in spec.detail.fields.items()
                    )
                )[:3]
                if passed_urls:
                    message = (
                        f"Detail fields already passed on {passed_urls!r}, but those URLs are not "
                        "linked by the candidate's current item root and detail href selector. "
                        "Verify that listing/link combination before repeating detail field discovery."
                    )
                elif not detail_urls:
                    message = (
                        "No detail URL was observed for the candidate's current item root and href "
                        "selector. Verify the listing/link combination before testing detail fields."
                    )
                else:
                    message = (
                        "Test every detail field with item_selector='html' on an observed detail URL. "
                        "Listing-page tests do not verify detail fields. Keep requested field names, "
                        "e.g. detail.fields.description; only avoid names already in listing fields."
                    )
                issues.append(Feedback(code="selector_failure", message=message))
        pagination = spec.pagination
        if pagination:
            if pagination.type in {"link", "click"}:
                control = require(
                    url,
                    pagination.next_selector,
                    "",
                    "href" if pagination.type == "link" else "",
                    pagination.type == "link",
                    nonempty=pagination.type == "link",
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
                    other_roots = sorted({
                        item for page, item, kind, previous_mode in self.advances
                        if page == url and item != spec.item_selector
                        and kind == pagination.type and previous_mode == mode
                    })[:3]
                    root_hint = (
                        f"Recorded progression uses different item roots {other_roots!r}; "
                        "it does not verify the candidate's current root. " if other_roots else ""
                    )
                    issues.append(
                        Feedback(
                            code="selector_failure",
                            message=(
                                root_hint + f"Verify {pagination.type}/{mode} record progression on the listing. "
                                "For append, click/scroll then wait_for_selector(min_count=N+1) or retest "
                                "the same item selector; for replace, retest fields after clicking."
                            ),
                        )
                    )
        return issues
