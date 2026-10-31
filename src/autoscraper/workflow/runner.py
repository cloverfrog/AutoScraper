# 任务主流程：独立解析需求 → 生成候选规则 → 校验/试抓取 → 必要时完整执行。
# 可修复的规则错误连同上轮候选反馈给 Agent；超时、导航失败和预算耗尽不会触发无效修复。
# 需求解析与所有修复共享主模型调用上限，语义等待另有预算，最终统一输出 TaskOutcome。
"""Bounded generate -> validate -> feedback -> repair workflow for the Agent CLI."""

import asyncio
import os
import time
from contextlib import nullcontext

from playwright.sync_api import Error as PlaywrightError
from autoscraper.browser.access import BrowserAccess, HandoffRequired

from autoscraper.agent.errors import (
    AgentError, InvalidSpecError, LLMBudgetExceeded, UnsupportedTaskError,
)
from autoscraper.agent.explorer import AgentSettings, generate_spec
from autoscraper.agent.clarification import (
    ClarificationHandler,
    ClarificationRequired,
    ClarificationSession,
)
from autoscraper.scraping.errors import (
    ContentTimeout, NavigationError, PaginationActionError, ScrapeError,
)
from autoscraper.scraping.executor import ScraperExecutor
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
# access 在探索、修复与执行之间共享最新认证快照，不把凭据放入 TaskOutcome 或模型反馈。
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
    access: BrowserAccess | None = None,
) -> TaskOutcome:
    from autoscraper.agent.explorer import build_task

    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    from autoscraper.agent.llm import make_llm, make_wait_llm

    build_task(url, task)  # 在付费解析需求之前先验证任务输入。
    policy = validation or ValidationSettings()
    llm = make_llm(settings)  # 独立需求解析和每轮修复共享同一付费调用预算。
    wait_llm = make_wait_llm(settings)
    reserve = settings.repair_reserve_calls if policy.max_repairs else 0
    # 需求解析也受首轮阶段上限约束，结构化重试不能侵占修复预留。
    llm.call_limit = settings.max_llm_calls - reserve
    deadline = time.monotonic() + settings.timeout_seconds
    clarification = ClarificationSession(handler=ask_user)
    access = access if access is not None else BrowserAccess()
    outcome = TaskOutcome(
        status="unfulfilled", original_url=url, clarifications=clarification.history,
        agent_step_limit=settings.max_steps, max_llm_calls=settings.max_llm_calls,
        repair_reserve_calls=reserve,
    )

    # 计算共享剩余时长并检查主模型调用预算；人工问答等待加回截止时间。
    def remaining():
        seconds = deadline + clarification.waiting_seconds + access.waiting_seconds - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Shared task time budget exhausted")
        if llm.calls >= settings.max_llm_calls:
            raise LLMBudgetExceeded("Shared task LLM request budget exhausted")
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
        except (TimeoutError, LLMBudgetExceeded):
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
        # 解析需求若已耗尽首轮额度，不启动无法发出请求的探索浏览器。
        if llm.calls >= llm.call_limit:
            raise LLMBudgetExceeded(
                f"Requirements consumed the initial request budget: used={llm.calls}, "
                f"phase_limit={llm.call_limit}, hard_limit={settings.max_llm_calls}"
            )
        # 试抓取、完整执行和各轮修复都沿用探索阶段的浏览器设置。
        executor = ScraperExecutor(
            headless=settings.headless, executable_path=settings.executable_path, access=access
        )
        # 首次生成也算一次尝试，因此总尝试数为 max_repairs + 1。
        previous_spec = None
        previous_evidence = None
        for attempt in range(policy.max_repairs + 1):
            # 保守要求修复至少有三次请求余量；这是启动策略，不是 Step 到请求数的换算。
            # 预算反馈放在前面，同时保留此前的具体原因，不用泛化错误覆盖诊断。
            if attempt and settings.max_llm_calls - llm.calls < 3:
                outcome.repair_not_started_reason = "insufficient_llm_requests"
                outcome.feedback = [
                    Feedback(
                        code="budget_exhausted",
                        recoverable=False,
                        message=f"Repair was not started: used={llm.calls}, "
                        f"hard_limit={settings.max_llm_calls}, minimum_remaining_requests=3",
                    ),
                    *outcome.feedback,
                ]
                return outcome
            # 新建修复 Agent，但沿用同一模型计数；只有工作流能开放预留额度。
            llm.call_limit = settings.max_llm_calls if attempt else settings.max_llm_calls - reserve
            llm.budget_blocked = False
            emit("budget.phase", phase="repair" if attempt else "exploration",
                 agent_step_limit=settings.max_steps, max_llm_calls=settings.max_llm_calls,
                 phase_request_limit=llm.call_limit, llm_calls=llm.calls,
                 repair_reserve_calls=reserve)
            with (
                labels(attempt=attempt + 1),
                operation("attempt"),
                operation("repair") if attempt else nullcontext(),
            ):
                seconds = remaining()
                stage: TaskStage = "exploration"
                outcome.failure_stage = None
                outcome.attempts = attempt + 1
                outcome.spec, outcome.data = None, None
                candidate = None
                repair_evidence = None
                result = None
                try:
                    # Step 上限独立于剩余请求数，只传递剩余任务时长。
                    run_settings = settings.model_copy(
                        update={
                            "timeout_seconds": max(1, min(settings.timeout_seconds, int(seconds))),
                        }
                    )
                    if attempt and previous_evidence is not None:
                        emit(
                            "repair.context",
                            previous_spec=previous_spec.model_dump(mode="json")
                            if previous_spec is not None else None,
                            evidence_summary=previous_evidence,
                        )
                    candidate = asyncio.run(
                        generate_spec(
                            url,
                            task,
                            run_settings,
                            requirements=requirements,
                            feedback=outcome.feedback,
                            previous_spec=previous_spec,
                            evidence_summary=previous_evidence,
                            llm=llm,
                            wait_llm=wait_llm,
                            clarification=clarification,
                            access=access,
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
                            result = executor.run(preview)
                        issues, complete = validate_preview_data(
                            result, requirements, candidate, preview, execute=execute
                        )
                        if not issues and execute and not complete:
                            stage = "execution"
                            with operation("execution"):
                                result = executor.run(candidate)
                            issues, complete = validate_execution_data(result, requirements)
                        outcome.sample_only = not complete
                    outcome.spec, outcome.data = candidate, result
                except HandoffRequired as exc:
                    outcome.status = "cancelled" if exc.cancelled else "needs_user"
                    outcome.pending_handoff = exc.request.model_dump()
                    return outcome
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
                except (NavigationError, ContentTimeout, PaginationActionError) as exc:
                    # 环境/等待失败不证明规则错误，保留候选供排查但停止修复。
                    issues = [Feedback(code=exc.code, message=str(exc), recoverable=False)]
                    outcome.spec, outcome.data = candidate, None
                except (ScrapeError, PlaywrightError) as exc:
                    # 剩余执行错误按可修复的选择器问题反馈，交由下一轮探索核对 DOM。
                    issues = [Feedback(code="selector_failure", message=str(exc)[:2000])]
                    outcome.spec, outcome.data = candidate, None
                except InvalidSpecError as exc:
                    candidate = exc.candidate
                    repair_evidence = exc.evidence_summary
                    issues = [Feedback(code="invalid_spec", message=str(exc)[:2000])]
                except LLMBudgetExceeded as exc:
                    # 单纯耗尽额度没有可修复的规则反馈，不能盲目花掉预留重新探索。
                    issues = [Feedback(
                        code="budget_exhausted", message=str(exc), recoverable=False,
                    )]
                    if attempt < policy.max_repairs:
                        outcome.repair_not_started_reason = (
                            "insufficient_llm_requests"
                            if settings.max_llm_calls - llm.calls < 3
                            else "no_recoverable_feedback"
                        )
                except AgentError as exc:
                    issues = [
                        Feedback(code="agent_failure", message=str(exc)[:2000], recoverable=False)
                    ]
                outcome.feedback = issues
                emit("validation.feedback", feedback=[item.model_dump() for item in issues],
                     failure_stage=stage)
                if candidate is not None and any(
                    item.code in {"navigation_timeout", "navigation_failure", "content_timeout",
                                  "pagination_action_failure"}
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
                previous_evidence = repair_evidence
        return outcome
    except (AgentError, TimeoutError, LLMBudgetExceeded) as exc:
        outcome.feedback = [
            Feedback(code="budget_exhausted", message=str(exc), recoverable=False),
            *outcome.feedback,
        ]
        return outcome
    finally:
        outcome.llm_calls = llm.calls
        outcome.wait_llm_calls = wait_llm.calls
