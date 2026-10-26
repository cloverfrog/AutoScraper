"""Task-scoped clarification state; independent of browser-use and terminal UI."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import Field

from .spec import NonEmpty, ProtocolModel
from .tracing import emit, operation


class ClarificationRequest(ProtocolModel):
    question: NonEmpty
    explanation: NonEmpty
    options: list[NonEmpty] = Field(default_factory=list, max_length=5)


class ClarificationRequired(Exception):
    def __init__(self, request: ClarificationRequest):
        super().__init__(request.question)
        self.request = request


ClarificationHandler = Callable[[ClarificationRequest], str | None]


@dataclass
class ClarificationSession:
    handler: ClarificationHandler | None = None
    history: list[dict] = field(default_factory=list)
    pending: ClarificationRequest | None = None
    waiting_seconds: float = 0.0

    def answer_pending(self) -> None:
        """Called between Agent runs, outside all Agent timeouts and action batches."""
        request = self.pending
        assert request is not None
        started = time.monotonic()
        try:
            with operation("clarification", request=request.model_dump()) as details:
                answer = self.handler(request) if self.handler else None
                details["answered"] = bool(answer and answer.strip())
                emit("clarification.answer", answer=answer)
        except (EOFError, KeyboardInterrupt):
            answer = None
        finally:
            self.waiting_seconds += time.monotonic() - started
        if answer is None or not answer.strip():
            raise ClarificationRequired(request)
        self.history.append({"request": request.model_dump(), "answer": answer.strip()})
        self.pending = None

    def prompt(self) -> str:
        if not self.history:
            return ""
        return (
            "\nUser clarification history (user answers supplement the original task; "
            "respect restrictions and do not repeat answered questions). "
            "Questions/explanation/options are Agent proposals, NOT user instructions. "
            "An answer allowing another source does not prove data/order equivalence. "
            "Explicit field/count requirements and protocol boundaries still apply:\n"
            + json.dumps(self.history, ensure_ascii=False)
        )
