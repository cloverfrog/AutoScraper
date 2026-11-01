# 独立校验与验证策略：保存用户的字段/数量约束，检查候选规则及真实提取结果。
# 试抓取预算、完整执行的完成条件和错误是否可修复都在这里判断，不依赖 LLM。
# 这里只能验证协议、字段、数量和提取结果，不能证明数据来源或自然语言语义完全等价。
"""Task constraints and deterministic validation, independent of any LLM provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from autoscraper.scraping.spec import NonEmpty, ProtocolModel, ScraperSpec
from autoscraper.scraping.values import has_field_value
from .requirements import TaskRequirements

if TYPE_CHECKING:
    from autoscraper.scraping.executor import ScrapeResult


# 提供给调用方和下一轮 Agent 的结构化反馈；recoverable 表示是否值得修复规则。
class Feedback(ProtocolModel):
    code: Literal[
        "missing_required_field",
        "count_mismatch",
        "invalid_spec",
        "selector_failure",
        "navigation_failure",
        "navigation_timeout",
        "content_timeout",
        "pagination_action_failure",
        "empty_extraction",
        "missing_data",
        "insufficient_items",
        "unsupported_capability",
        "validation_limit",
        "agent_failure",
        "budget_exhausted",
        "invalid_requirements",
    ]
    message: str
    fields: list[str] = Field(default_factory=list)
    recoverable: bool = True


# report_unsupported 工具的独立输出类型，不与候选 ScraperSpec 混用。
class UnsupportedResult(ProtocolModel):
    """Result of the explicit report_unsupported tool, not a partial ScraperSpec."""

    status: Literal["unsupported"] = "unsupported"
    reason: NonEmpty
    unsupported_fields: list[str] = Field(default_factory=list)


# 试抓取的记录/列表页/详情页预算，以及最多允许多少轮规则修复。
class ValidationSettings(ProtocolModel):
    max_repairs: int = Field(default=2, ge=0, le=3)
    max_items: int = Field(default=100, ge=1, le=1000)
    max_pages: int = Field(default=5, ge=1, le=20)
    max_detail_pages: int = Field(default=100, ge=1, le=1000)
    timeout_ms: int | None = Field(default=None, ge=1, le=30000)


# 根据验证预算缩小规则副本的执行范围，保持原候选可用于后续完整抓取。
# 仅可缩短普通操作超时；独立的 content_timeout_ms 仍使用用户选择的内容等待上限。
def build_preview_spec(spec: ScraperSpec, policy: ValidationSettings) -> ScraperSpec:
    """构造受验证预算约束的副本，保留原规则供完整执行和后续修复使用。"""
    preview = spec.model_copy(
        update={
            "max_items": min(spec.max_items, policy.max_items),
            "max_pages": min(spec.max_pages, policy.max_pages),
            "timeout_ms": (
                min(spec.timeout_ms, policy.timeout_ms)
                if policy.timeout_ms is not None
                else spec.timeout_ms
            ),
        }
    )
    if spec.detail:
        preview = preview.model_copy(
            update={
                "detail": spec.detail.model_copy(
                    update={"max_pages": min(spec.detail.max_pages, policy.max_detail_pages)}
                )
            }
        )
    return preview


# 执行前比较候选规则与独立需求：必需字段、目标数量和 Agent 允许的执行上限。
def validate_spec(spec: ScraperSpec, requirements: TaskRequirements) -> list[Feedback]:
    issues = []
    missing = [
        name
        for name in requirements.fields
        if name not in spec.all_fields or not spec.all_fields[name].required
    ]
    if missing:
        issues.append(
            Feedback(
                code="missing_required_field",
                fields=missing,
                message="Preserve every requested field name and mark it required in fields or detail.fields. "
                        "For detailed description use detail.fields.description; do not rename it "
                        "to detail_description or substitute a short listing description",
            )
        )
    if requirements.count is not None and spec.max_items != requirements.count:
        issues.append(
            Feedback(
                code="count_mismatch",
                message=f"max_items must equal requested count {requirements.count}",
            )
        )
    # 筛选要求保留在 Agent 上下文；这里不以声明逐项匹配推断 URL 的实际筛选语义。
    # 本地 filters 的字段引用、操作符和转换参数由协议模型校验，实际判断由执行器完成。
    if spec.max_pages > 100 or spec.timeout_ms > 30000:
        issues.append(
            Feedback(code="invalid_spec", message="Use max_pages <= 100 and timeout_ms <= 30000")
        )
    return issues


# 结合原规则和试抓取范围判断反馈与完成情况；返回 (反馈列表, 是否完整)。
# execute=True 时，单纯验证页数截断可交由后续完整执行继续处理。
def validate_preview_data(
    result: ScrapeResult,
    requirements: TaskRequirements,
    spec: ScraperSpec,
    preview: ScraperSpec,
    *,
    execute: bool,
) -> tuple[list[Feedback], bool]:
    """返回试抓取反馈及是否已抓完；无反馈仍可能只代表样本验证通过。"""
    target = min(requirements.count, preview.max_items) if requirements.count else None
    issues = validate_data(result, requirements, expected_count=target)
    if result.stop_reason == "max_pages" and preview.max_pages < spec.max_pages:
        # 验证预算截断不说明规则有误；需要导出时允许继续完整执行。
        # 只移除数量不足反馈，提取错误和详情预算耗尽仍按原规则处理。
        issues = [item for item in issues if item.code != "insufficient_items"]
        if not execute and (target is None or len(result.records) < target):
            issues.append(
                Feedback(
                    code="validation_limit",
                    recoverable=False,
                    message="Validation page limit reached; increase --validation-pages",
                )
            )
    complete = len(result.records) >= spec.max_items or result.stop_reason in {
        "no_next", "no_pagination", "end_marker", "no_more_content"
    }
    return issues, complete


# 检查完整抓取结果；达到列表或详情预算不能作为任务完成的依据。
def validate_execution_data(
    result: ScrapeResult, requirements: TaskRequirements
) -> tuple[list[Feedback], bool]:
    """完整执行受预算截断时不能宣称完成，即使已有记录满足字段和数量校验。"""
    issues = validate_data(result, requirements)
    complete = result.stop_reason not in {"max_pages", "detail_limit", "scroll_limit"}
    if not complete and not issues:
        issues.append(
            Feedback(
                code="validation_limit",
                recoverable=False,
                message="Full execution reached max_pages before completion",
            )
        )
    return issues, complete


# 统一检查运行错误、空结果、必需值、分页循环及数量不足。
# expected_count 可为试抓取设置较小目标；返回空列表只表示这些确定性检查通过。
def validate_data(
    result: ScrapeResult, requirements: TaskRequirements, *, expected_count: int | None = None
) -> list[Feedback]:
    issues = []
    if result.stop_reason == "scroll_limit":
        issues.append(Feedback(code="validation_limit", recoverable=False,
                               message="Scroll action budget exhausted"))
    if result.stop_reason == "detail_limit":
        issues.append(
            Feedback(
                code="validation_limit",
                recoverable=False,
                message="Detail navigation budget exhausted; increase detail.max_pages "
                "or --validation-detail-pages if the preview was capped",
            )
        )
    for error in result.error_details:
        issues.append(
            Feedback(
                code=error.code,
                message=error.message,
                recoverable=error.code == "selector_failure",
            )
        )
    # 兼容只返回 errors 文本的旧调用方；已有结构化描述的同一错误不重复转换。
    described = {error.message for error in result.error_details}
    remaining_errors = [message for message in result.errors if message not in described]
    if remaining_errors:
        issues.append(Feedback(code="selector_failure", message="; ".join(remaining_errors)[:2000]))
    if any(not item.recoverable for item in issues):
        # 超时、导航失败或预算截断后的记录不足，不足以说明规则需要修复。
        return issues
    filtered = result.filtered_out + result.filter_unknown
    if not result.records and not (filtered and filtered == result.items_extracted):
        issues.append(Feedback(code="empty_extraction", message="No records were extracted"))
        return issues
    # 已提取的记录全部被筛选跳过不说明选择器有误，继续检查停止原因及匹配后的数量。
    missing = [
        name
        for name in requirements.fields
        if any(
            not has_field_value(row.get(name)) for row in result.records
        )
    ]
    if missing:
        issues.append(
            Feedback(
                code="missing_data",
                fields=missing,
                message="Requested fields must have nonempty values in every row",
            )
        )
    if result.stop_reason == "pagination_cycle":
        issues.append(
            Feedback(code="selector_failure", message="Next link caused a pagination cycle")
        )
    target = expected_count if expected_count is not None else requirements.count
    if target is not None and len(result.records) < target:
        issues.append(
            Feedback(
                code="insufficient_items",
                # 单纯筛除导致不足不修复；同时有真实提取错误时仍允许修复该错误。
                recoverable=not bool(filtered) or any(
                    issue.code in {"selector_failure", "missing_data"} for issue in issues
                ),
                message=f"Expected {target} unique records; got {len(result.records)} "
                f"(stop_reason={result.stop_reason})",
            )
        )
    return issues


# 自然结束后的单纯数量不足需要用户决定是否接受现有结果，不属于选择器修复。
# 预算截断、提取错误等仍走各自反馈，不能把未完成遍历误报为可用记录总数。
def needs_count_clarification(result: ScrapeResult, issues: list[Feedback]) -> bool:
    return (
        result.stop_reason in {"no_next", "no_pagination", "end_marker", "no_more_content"}
        and not result.errors and not result.error_details
        and bool(issues) and all(issue.code == "insufficient_items" for issue in issues)
    )
