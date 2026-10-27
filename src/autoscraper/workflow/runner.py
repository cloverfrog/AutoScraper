# 任务主流程：独立解析需求 → 生成候选规则 → 校验/试抓取 → 必要时完整执行。
# 可修复的规则错误连同上轮候选反馈给 Agent；超时、导航失败和预算耗尽不会触发无效修复。
# 需求解析与所有修复共享主模型调用上限，语义等待另有预算，最终统一输出 TaskOutcome。
"""Bounded generate -> validate -> feedback -> repair workflow for the Agent CLI."""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
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
    ScrapeResult,
    ScraperExecutor,
)
from autoscraper.scraping.spec import ScraperSpec
from .validation import (
    Feedback,
    TaskRequirements,
    ValidationSettings,
    build_preview_spec,
    validate_execution_data,
    validate_preview_data,
    validate_spec,
)
from autoscraper.observability.tracing import Trace, emit, labels, operation, traced_task


# 任务全过程的结果容器：状态、候选规则、数据、反馈、预算使用和澄清记录。
# spec 可能是在执行失败后保留的候选，是否完成任务应结合 status 和 sample_only 判断。
@dataclass
class TaskOutcome:
    # status 是任务终态；spec/data 可以保留失败现场，不能仅凭它们存在判断成功。
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
    # history 记录各轮验证反馈；clarifications 是跨修复轮次共享的用户问答。
    history: list[dict] = field(default_factory=list)
    original_url: str | None = None
    clarifications: list[dict] = field(default_factory=list)
    pending_clarification: dict | None = None

    # 生成 CLI/追踪共用的 JSON 摘要；区分 Agent 返回、实际执行和任务是否完成。
    # spec_valid 目前仅表示已得到协议模型，不能单凭它判断需求和数据验证是否通过。
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
            "fulfillment_basis": "clarified_task" if self.clarifications else "original_task",
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
            "original_url": self.original_url,
            "actual_start_url": str(self.spec.start_url) if self.spec else None,
            "clarifications": self.clarifications,
            "pending_clarification": self.pending_clarification,
            "validation_scope": "protocol, required fields, count and extraction; "
            "source equivalence and semantic constraints are not independently verified",
        }


# 从有限的中英文表达中保留明确字段名与数量；显式参数优先于文本推断。
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


# 先解析确定的字段/数量，只把缺失部分交给模型；不向它提供生成后的规则。
# 模型返回后再次应用显式值并校验，防止生成规则反过来缩减用户原始需求。
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
    from autoscraper.agent.tools import make_llm, make_wait_llm

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
                stage = "exploration"
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
