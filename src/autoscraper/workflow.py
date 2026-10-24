"""Bounded generate -> validate -> feedback -> repair workflow for the Agent CLI."""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError

from .agent import AgentError, AgentSettings, InvalidSpecError, UnsupportedTaskError, generate_spec
from .executor import ContentTimeout, NavigationError, ScrapeError, ScrapeResult, ScraperExecutor
from .spec import ScraperSpec
from .validation import Feedback, TaskRequirements, ValidationSettings, validate_data, validate_spec


@dataclass
class TaskOutcome:
    status: str
    requirements: TaskRequirements | None = None
    spec: ScraperSpec | None = None
    data: ScrapeResult | None = None
    feedback: list[Feedback] = field(default_factory=list)
    attempts: int = 0
    llm_calls: int = 0
    wait_llm_calls: int = 0
    sample_only: bool = False
    failure_stage: str | None = None
    history: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "status": self.status,
            "agent_status": "completed"
            if self.spec is not None or self.status == "unsupported"
            else "not_completed",
            "failure_stage": self.failure_stage,
            "spec_valid": self.spec is not None,
            "execution_success": self.data is not None
            and not self.data.errors
            and self.data.stop_reason != "detail_limit",
            "task_fulfilled": self.status in {"success", "validated"} and not self.sample_only,
            "sample_only": self.sample_only,
            "requirements": self.requirements.model_dump() if self.requirements else None,
            "attempts": self.attempts,
            "llm_calls": self.llm_calls,
            "wait_llm_calls": self.wait_llm_calls,
            "items": len(self.data.records) if self.data else 0,
            "pages": self.data.pages_visited if self.data else 0,
            "detail_pages": self.data.detail_pages_visited if self.data else 0,
            "stop_reason": self.data.stop_reason if self.data else None,
            "feedback": [item.model_dump() for item in self.feedback],
            "validation_history": self.history,
        }


def explicit_requirements(task: str, fields: list[str] | None, count: int | None):
    """Preserve explicitly named identifiers independently of generated rules."""
    match = re.search(
        r"(?:字段名(?:称)?\s*(?:为|是|[:：])|fields?\s+(?:named|names?\s*[:：]))\s*"
        r"([A-Za-z_][A-Za-z0-9_]*(?:(?:\s*[,，、]\s*(?:and\s+)?|\s+and\s+|\s*[和及]\s*)"
        r"[A-Za-z_][A-Za-z0-9_]*)*)",
        task,
        re.IGNORECASE,
    )
    if fields is None and match:
        fields = re.split(
            r"\s*[,，、]\s*(?:and\s+)?|\s+and\s+|\s*[和及]\s*", match.group(1), flags=re.IGNORECASE
        )
    if count is None:
        number = re.search(
            r"(?:前|first\s+)\s*(\d+)|"
            r"(\d+)\s*(?:本|条|个|books?\b|records?\b|items?\b)",
            task,
            re.IGNORECASE,
        )
        if number:
            count = int(number.group(1) or number.group(2))
    return fields, count


async def extract_requirements(task: str, llm, fields=None, count=None) -> TaskRequirements:
    fields, count = explicit_requirements(task, fields, count)
    if fields is not None and count is not None:
        return TaskRequirements(fields=fields, count=count)
    from browser_use.llm.messages import SystemMessage, UserMessage

    response = await llm.ainvoke(
        [
            SystemMessage(
                content="Extract the user's required field names and requested record count. "
                "Do not see or use any generated scraping rule. Preserve exact field names "
                "when given; otherwise choose short descriptive names. Count=null if absent. "
                "All requested fields are mandatory. Only mark unsupported_capabilities when "
                "the user EXPLICITLY requires JS-only pagination, infinite scroll or login. "
                "One level of detail-page extraction is supported. "
                "Do not mark detail-page crawling as unsupported."
            ),
            UserMessage(content=json.dumps({"task": task}, ensure_ascii=False)),
        ],
        output_format=TaskRequirements,
    )
    result = TaskRequirements.model_validate(response.completion)
    if count is not None or fields is not None:
        result = result.model_copy(
            update={
                "count": count if count is not None else result.count,
                "fields": fields if fields is not None else result.fields,
            }
        )
        result = TaskRequirements.model_validate(result.model_dump())
    return result


def run_task(
    url: str,
    task: str,
    settings: AgentSettings,
    *,
    validation: ValidationSettings | None = None,
    fields: list[str] | None = None,
    count: int | None = None,
    execute: bool = False,
) -> TaskOutcome:
    from .agent import build_task

    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    from .agent_tools import make_llm, make_wait_llm

    build_task(url, task)  # Validate input before paying for requirement extraction.
    policy = validation or ValidationSettings()
    llm = make_llm(settings)  # One paid-call budget shared by extraction and every repair.
    wait_llm = make_wait_llm(settings)
    deadline = time.monotonic() + settings.timeout_seconds
    outcome = TaskOutcome(status="unfulfilled")

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0 or llm.calls >= settings.max_steps:
            raise AgentError("Shared time or LLM call budget exhausted")
        return seconds

    async def requirements_call():
        async with asyncio.timeout(remaining()):
            return await extract_requirements(task, llm, fields, count)

    try:
        try:
            outcome.requirements = asyncio.run(requirements_call())
        except TimeoutError:
            raise
        except Exception as exc:
            outcome.feedback = [
                Feedback(code="invalid_requirements", message=str(exc)[:2000], recoverable=False)
            ]
            return outcome
        requirements = outcome.requirements
        if requirements.unsupported_capabilities:
            outcome.status = "unsupported"
            outcome.feedback = [
                Feedback(
                    code="unsupported_capability",
                    recoverable=False,
                    message="Current ScraperSpec cannot represent: "
                    + ", ".join(requirements.unsupported_capabilities),
                )
            ]
            return outcome
        previous_spec = None
        for attempt in range(policy.max_repairs + 1):
            seconds = remaining()
            stage = "exploration"
            outcome.failure_stage = None
            outcome.attempts = attempt + 1
            outcome.spec, outcome.data = None, None
            candidate = None
            result = None
            try:
                run_settings = settings.model_copy(
                    update={
                        "max_steps": settings.max_steps - llm.calls,
                        "timeout_seconds": max(1, min(settings.timeout_seconds, int(seconds))),
                    }
                )
                candidate = asyncio.run(
                    generate_spec(
                        url,
                        task,
                        run_settings,
                        requirements=requirements,
                        feedback=outcome.feedback,
                        previous_spec=previous_spec,
                        llm=llm,
                        wait_llm=wait_llm,
                    )
                )
                candidate = candidate.model_copy(
                    update={"content_timeout_ms": settings.content_timeout_ms}
                )
                issues = validate_spec(candidate, requirements)
                if not issues:
                    preview = candidate.model_copy(
                        update={
                            "max_items": min(candidate.max_items, policy.max_items),
                            "max_pages": min(candidate.max_pages, policy.max_pages),
                            "timeout_ms": (
                                min(candidate.timeout_ms, policy.timeout_ms)
                                if policy.timeout_ms is not None
                                else candidate.timeout_ms
                            ),
                        }
                    )
                    if candidate.detail:
                        preview = preview.model_copy(
                            update={
                                "detail": candidate.detail.model_copy(
                                    update={
                                        "max_pages": min(
                                            candidate.detail.max_pages, policy.max_detail_pages
                                        )
                                    }
                                )
                            }
                        )
                    stage = "validation"
                    result = ScraperExecutor().run(preview)
                    target = (
                        min(requirements.count, policy.max_items) if requirements.count else None
                    )
                    issues = validate_data(result, requirements, expected_count=target)
                    if (
                        result.stop_reason == "max_pages"
                        and preview.max_pages < candidate.max_pages
                    ):
                        issues = [item for item in issues if item.code != "insufficient_items"]
                        if not execute and (target is None or len(result.records) < target):
                            issues.append(
                                Feedback(
                                    code="validation_limit",
                                    recoverable=False,
                                    message="Validation page limit reached; increase --validation-pages",
                                )
                            )
                    complete = len(result.records) >= candidate.max_items or result.stop_reason in {
                        "no_next",
                        "no_pagination",
                    }
                    if not issues and execute and not complete:
                        stage = "execution"
                        result = ScraperExecutor().run(candidate)
                        issues = validate_data(result, requirements)
                        complete = result.stop_reason not in {"max_pages", "detail_limit"}
                        if not complete and not issues:
                            issues.append(
                                Feedback(
                                    code="validation_limit",
                                    recoverable=False,
                                    message="Full execution reached max_pages before completion",
                                )
                            )
                    outcome.sample_only = not complete
                outcome.spec, outcome.data = candidate, result
            except UnsupportedTaskError as exc:
                outcome.status = "unsupported"
                outcome.feedback = [
                    Feedback(
                        code="unsupported_capability",
                        message=str(exc),
                        fields=exc.fields,
                        recoverable=False,
                    )
                ]
                outcome.history.append(
                    {
                        "attempt": attempt + 1,
                        "feedback": [item.model_dump() for item in outcome.feedback],
                    }
                )
                return outcome
            except (NavigationError, ContentTimeout) as exc:
                issues = [Feedback(code=exc.code, message=str(exc), recoverable=False)]
                outcome.spec, outcome.data = candidate, None
            except (ScrapeError, PlaywrightError) as exc:
                issues = [Feedback(code="selector_failure", message=str(exc)[:2000])]
                outcome.spec, outcome.data = candidate, None
            except InvalidSpecError as exc:
                issues = [Feedback(code="invalid_spec", message=str(exc)[:2000])]
            except AgentError as exc:
                issues = [
                    Feedback(code="agent_failure", message=str(exc)[:2000], recoverable=False)
                ]
            outcome.feedback = issues
            if candidate is not None and any(
                item.code in {"navigation_timeout", "navigation_failure", "content_timeout"}
                for item in issues
            ):
                outcome.failure_stage = stage
            outcome.history.append(
                {"attempt": attempt + 1, "feedback": [item.model_dump() for item in issues]}
            )
            if not issues:
                outcome.status = "success" if execute else "validated"
                return outcome
            if any(not item.recoverable for item in issues):
                break
            previous_spec = candidate
        return outcome
    except (AgentError, TimeoutError) as exc:
        outcome.feedback = [Feedback(code="budget_exhausted", message=str(exc), recoverable=False)]
        return outcome
    finally:
        outcome.llm_calls = llm.calls
        outcome.wait_llm_calls = wait_llm.calls
