"""Task constraints and deterministic validation, independent of any LLM provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator

from .spec import NonEmpty, ProtocolModel, ScraperSpec

if TYPE_CHECKING:
    from .executor import ScrapeResult


class TaskRequirements(ProtocolModel):
    fields: list[NonEmpty] = Field(min_length=1, max_length=50)
    count: int | None = Field(default=None, ge=1, le=1_000_000)
    unsupported_capabilities: list[Literal["javascript_pagination", "infinite_scroll", "login"]] = (
        Field(default_factory=list)
    )

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, values):
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("Requested field names must be nonblank and unique")
        return values


class Feedback(ProtocolModel):
    code: Literal[
        "missing_required_field",
        "count_mismatch",
        "invalid_spec",
        "selector_failure",
        "navigation_failure",
        "navigation_timeout",
        "content_timeout",
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


class UnsupportedResult(ProtocolModel):
    """Result of the explicit report_unsupported tool, not a partial ScraperSpec."""

    status: Literal["unsupported"] = "unsupported"
    reason: NonEmpty
    unsupported_fields: list[str] = Field(default_factory=list)


class ValidationSettings(ProtocolModel):
    max_repairs: int = Field(default=2, ge=0, le=3)
    max_items: int = Field(default=100, ge=1, le=1000)
    max_pages: int = Field(default=5, ge=1, le=20)
    max_detail_pages: int = Field(default=100, ge=1, le=1000)
    timeout_ms: int | None = Field(default=None, ge=1, le=30000)


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
                message="Include every requested field and mark it required",
            )
        )
    if requirements.count is not None and spec.max_items != requirements.count:
        issues.append(
            Feedback(
                code="count_mismatch",
                message=f"max_items must equal requested count {requirements.count}",
            )
        )
    if spec.max_pages > 100 or spec.timeout_ms > 30000:
        issues.append(
            Feedback(code="invalid_spec", message="Use max_pages <= 100 and timeout_ms <= 30000")
        )
    return issues


def validate_data(
    result: ScrapeResult, requirements: TaskRequirements, *, expected_count: int | None = None
) -> list[Feedback]:
    issues = []
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
    # Keep compatibility with callers that only supply the original string errors.
    described = {error.message for error in result.error_details}
    remaining_errors = [message for message in result.errors if message not in described]
    if remaining_errors:
        issues.append(Feedback(code="selector_failure", message="; ".join(remaining_errors)[:2000]))
    if any(not item.recoverable for item in issues):
        # Missing rows after a timeout, navigation failure or budget limit do not justify repair.
        return issues
    if not result.records:
        issues.append(Feedback(code="empty_extraction", message="No records were extracted"))
        return issues
    missing = [
        name
        for name in requirements.fields
        if any(
            not isinstance(row.get(name), str) or not row[name].strip() for row in result.records
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
                message=f"Expected {target} unique records; got {len(result.records)} "
                f"(stop_reason={result.stop_reason})",
            )
        )
    return issues
