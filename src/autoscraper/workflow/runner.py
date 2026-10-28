# 任务主流程：独立解析需求 → 生成候选规则 → 校验/试抓取 → 必要时完整执行。
# 可修复的规则错误连同上轮候选反馈给 Agent；超时、导航失败和预算耗尽不会触发无效修复。
# 需求解析与所有修复共享主模型调用上限，语义等待另有预算，最终统一输出 TaskOutcome。
"""Bounded generate -> validate -> feedback -> repair workflow for the Agent CLI."""

import asyncio
import os
import time
from contextlib import nullcontext

from playwright.sync_api import Error as PlaywrightError

from autoscraper.agent.explorer import (
    AgentError,
    AgentSettings,
    InvalidSpecError,
    UnsupportedTaskError,
    generate_spec,
)
from autoscraper.agent.clarification import (
    ClarificationHandler,
    ClarificationRequired,
    ClarificationSession,
)
from autoscraper.scraping.executor import (
    ContentTimeout,
    NavigationError,
    ScrapeError,
    ScraperExecutor,
)
from .outcome import TaskOutcome, TaskStage
from .requirements import extract_requirements
from .validation import (
    Feedback,
    ValidationSettings,
    build_preview_spec,
    validate_execution_data,
    validate_preview_data,
    validate_spec,
)
from autoscraper.observability.tracing import Trace, emit, labels, operation, traced_task


# 同步任务入口：在有限预算内完成需求解析、探索、验证和可选完整抓取。
# execute=False 也会试抓取，但可返回 sample_only；execute=True 需要完整数据验证通过。
# ask_user 注入交互方式；trace 由装饰器管理，函数最终返回 TaskOutcome 而不写产物文件。
@traced_task
def run_task(
    url: str,
    task: str,
    settings: AgentSettings,
    *,
    validation: ValidationSettings | None = None,
    fields: list[str] | None = None,
    count: int | None = None,
    execute: bool = False,
    ask_user: ClarificationHandler | None = None,
    trace: Trace | None = None,
) -> TaskOutcome:
    from autoscraper.agent.explorer import build_task

    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    from autoscraper.agent.llm import make_llm, make_wait_llm

    build_task(url, task)  # 在付费解析需求之前先验证任务输入。
    policy = validation or ValidationSettings()
    llm = make_llm(settings)  # 独立需求解析和每轮修复共享同一付费调用预算。
    wait_llm = make_wait_llm(settings)
    deadline = time.monotonic() + settings.timeout_seconds
    clarification = ClarificationSession(handler=ask_user)
    outcome = TaskOutcome(
        status="unfulfilled", original_url=url, clarifications=clarification.history
    )

    # 计算共享剩余时长并检查主模型调用预算；人工问答等待加回截止时间。
    def remaining():
        seconds = deadline + clarification.waiting_seconds - time.monotonic()
        if seconds <= 0 or llm.calls >= settings.max_steps:
            raise AgentError("Shared time or LLM call budget exhausted")
        return seconds

    # 为独立需求解析套用追踪和剩余时间限制，调用量计入共享主模型预算。
    async def requirements_call():
        with labels(source="requirements"), operation("requirements") as details:
            async with asyncio.timeout(remaining()):
                result = await extract_requirements(task, llm, fields, count)
                details.update(result.model_dump())
                return result

    # 先建立独立需求基准，后续生成和修复都必须满足它，不能用候选规则反推需求。
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
        # 首次生成也算一次尝试，因此总尝试数为 max_repairs + 1。
        previous_spec = None
        for attempt in range(policy.max_repairs + 1):
            with (labels(attempt=attempt + 1), operation("attempt"),
                  operation("repair") if attempt else nullcontext()):
                seconds = remaining()
                stage: TaskStage = "exploration"
                outcome.failure_stage = None
                outcome.attempts = attempt + 1
                outcome.spec, outcome.data = None, None
                candidate = None
                result = None
                try:
                    # 复用模型实例，并把剩余步骤/时间交给这一轮；修复不会重置总预算。
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
                            clarification=clarification,
                        )
                    )
                    candidate = candidate.model_copy(
                        update={"content_timeout_ms": settings.content_timeout_ms}
                    )
                    emit("spec.candidate", spec=candidate.model_dump(mode="json"))
                    with operation("validation") as validation_details:
                        issues = validate_spec(candidate, requirements)
                        validation_details["feedback"] = [item.model_dump() for item in issues]
                    if not issues:
                        # 先用较小预算验证提取；只有样本无错误且尚未抓完时才完整执行。
                        preview = build_preview_spec(candidate, policy)
                        stage = "validation"
                        with operation("validation_extraction"):
                            result = ScraperExecutor().run(preview)
                        issues, complete = validate_preview_data(
                            result, requirements, candidate, preview, execute=execute
                        )
                        if not issues and execute and not complete:
                            stage = "execution"
                            with operation("execution"):
                                result = ScraperExecutor().run(candidate)
                            issues, complete = validate_execution_data(result, requirements)
                        outcome.sample_only = not complete
                    outcome.spec, outcome.data = candidate, result
                except ClarificationRequired as exc:
                    outcome.status = "needs_clarification"
                    outcome.pending_clarification = exc.request.model_dump()
                    return outcome
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
                    # 环境/等待失败不证明规则错误，保留候选供排查但停止修复。
                    issues = [Feedback(code=exc.code, message=str(exc), recoverable=False)]
                    outcome.spec, outcome.data = candidate, None
                except (ScrapeError, PlaywrightError) as exc:
                    # 剩余执行错误按可修复的选择器问题反馈，交由下一轮探索核对 DOM。
                    issues = [Feedback(code="selector_failure", message=str(exc)[:2000])]
                    outcome.spec, outcome.data = candidate, None
                except InvalidSpecError as exc:
                    issues = [Feedback(code="invalid_spec", message=str(exc)[:2000])]
                except AgentError as exc:
                    issues = [
                        Feedback(code="agent_failure", message=str(exc)[:2000], recoverable=False)
                    ]
                outcome.feedback = issues
                emit("validation.feedback", feedback=[item.model_dump() for item in issues],
                     failure_stage=stage)
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
                # 传回失败规则与具体反馈，让下一轮围绕已知问题修复而非从零猜测。
                previous_spec = candidate
        return outcome
    except (AgentError, TimeoutError) as exc:
        outcome.feedback = [Feedback(code="budget_exhausted", message=str(exc), recoverable=False)]
        return outcome
    finally:
        outcome.llm_calls = llm.calls
        outcome.wait_llm_calls = wait_llm.calls
