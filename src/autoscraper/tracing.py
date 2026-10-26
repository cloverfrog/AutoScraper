"""Task-local, bounded observability. No browser-use or CLI dependency."""

import functools
import json
import logging
import math
import re
import sys
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from itertools import islice
from urllib.parse import urlsplit


_current = ContextVar("autoscraper_trace", default=None)
_labels = ContextVar("autoscraper_trace_labels", default={})
_usage = ContextVar("autoscraper_trace_usage", default=None)


def current_trace():
    return _current.get()


@contextmanager
def labels(**values):
    token = _labels.set({**_labels.get(), **values})
    try:
        yield
    finally:
        _labels.reset(token)


def emit(event_type, **payload):
    trace = current_trace()
    if trace is not None:
        trace.emit(event_type, payload)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Sanitizer:
    """Default-deny strings, plus explicit rules for diagnostic parameters.

    Free text and arbitrary webpage/LLM output are represented by size, never
    content. URLs retain origin only; CSS quoted literals are removed. Known
    credentials are also removed from allowed strings, including dictionary keys.
    """

    SAFE = {
        "status",
        "code",
        "type",
        "source",
        "model",
        "failure_stage",
        "stop_reason",
        "agent_status",
        "fulfillment_basis",
        "name",
        "tool",
        "attribute",
        "exception",
        "reason",
        "event_type",
        "level",
        "logger",
        "outcome",
        "termination",
        "error_codes",
    }
    NUMERIC = {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cached_tokens"}

    def __init__(self, secrets=()):
        self.secrets = tuple(value for value in secrets if value)

    def text(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"[\x00-\x1f\x7f\ud800-\udfff]", " ", value)
        return value[:256]

    def clean(self, value, key="", depth=0, budget=None):
        budget = [500] if budget is None else budget
        budget[0] -= 1
        if depth > 8 or budget[0] < 0:
            return "[depth limit]"
        if any(
            part in key.lower()
            for part in (
                "api_key",
                "authorization",
                "cookie",
                "password",
                "secret",
                "header",
            )
        ):
            return "[redacted]"
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if key.endswith("url") or key == "url":
                try:
                    parsed = urlsplit(value)
                    if parsed.scheme in {"https", "http"} and parsed.hostname:
                        return self.text(f"{parsed.scheme}://{parsed.hostname}")
                except ValueError:
                    pass
                return "[redacted URL]"
            if "selector" in key:
                # Attribute values and :contains-like literals can contain page data.
                return self.text(re.sub(r"(['\"]).*?\1", "'[redacted]'", value))
            if key in self.SAFE:
                return self.text(value)
            return {"redacted": True, "chars": len(value)}
        if isinstance(value, dict):
            return {
                self.text(str(k)): self.clean(v, str(k), depth + 1, budget)
                for k, v in islice(value.items(), 50)
                if budget[0] > 0
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(v, key, depth + 1, budget) for v in value[:50] if budget[0] > 0]
        return {"type": type(value).__name__}


class FileSink:
    """One run directory; flushed appends, finite total byte budget, no buffers."""

    def __init__(self, root=Path(".agent-runtime/runs"), *, max_bytes=20_000_000):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.written = 0
        self.directory = None

    def start(self, run_id):
        self.directory = self.root / run_id
        self.directory.mkdir(parents=True, mode=0o700)
        (self.directory / "history").mkdir(mode=0o700)

    def _write(self, path, data, *, append=False, reserve=False):
        raw = data.encode("utf-8")
        # Reserve space for the final summary after event/console quota exhaustion.
        limit = self.max_bytes if reserve else max(0, self.max_bytes - 65536)
        if self.written + len(raw) > limit:
            raise OSError("Trace size limit reached")
        mode = "a" if append else "w"
        import os

        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, mode, encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
        self.written += len(raw)

    def event(self, event):
        line = json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        self._write(self.directory / "events.jsonl", line, append=True)
        self._write(self.directory / "console.log", line, append=True)

    def console(self, entry):
        self._write(
            self.directory / "console.log",
            json.dumps(entry, ensure_ascii=False) + "\n",
            append=True,
        )

    def history(self, attempt, entry):
        # JSONL snapshots during execution, without a growing in-memory array.
        filename = f"attempt-{attempt}.jsonl"
        self._write(
            self.directory / "history" / filename,
            json.dumps(entry, ensure_ascii=False) + "\n",
            append=True,
        )

    def summary(self, summary):
        path = self.directory / "summary.json"
        temporary = path.with_suffix(".tmp")
        self._write(
            temporary, json.dumps(summary, ensure_ascii=False, indent=2) + "\n", reserve=True
        )
        temporary.replace(path)


class _LogHandler(logging.Handler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def emit(self, record):
        if current_trace() is self.trace:
            # Do not format arbitrary messages, exception text or traceback locals.
            self.trace.console(
                {
                    "level": record.levelname,
                    "logger": record.name,
                    "message": "[framework log content omitted]",
                }
            )


class Trace:
    """Reusable recorder. Consumers receive sanitized dictionaries only.

    Consumer errors disable that consumer and warn once; counters remain available.
    ContextVars isolate concurrent tasks; locking serializes writers sharing a run.
    """

    def __init__(self, *sinks, secrets=()):
        self.run_id = uuid.uuid4().hex
        self.sanitizer = Sanitizer(secrets)
        self.sinks = list(sinks)
        self.failed = set()
        self.errors = []
        self.seq = 0
        self.counts = Counter()
        self.llm_sources = Counter()
        self.tokens = Counter()
        self.usage_calls = 0
        self.durations = Counter()
        self.outcome = None
        self.exit_code = None
        self.metadata = {}
        self.started = None
        self.active = False
        self.lock = threading.RLock()

    def _send(self, method, *args):
        for index, sink in enumerate(self.sinks):
            if index in self.failed and method != "summary":
                continue
            try:
                callback = getattr(sink, method, None)
                if callback is not None:
                    callback(*args)
            except Exception:
                if index not in self.failed:
                    self.failed.add(index)
                    self.errors.append({"consumer": index, "code": "trace_write_failure"})
                    # Never include filesystem errors or consumer exception messages.
                    try:
                        print(
                            "AutoScraper: trace consumer failed or reached size limit; "
                            "task continues, trace is incomplete.",
                            file=sys.stderr,
                        )
                    except Exception:
                        pass

    def emit(self, event_type, payload=None):
        with self.lock:
            event_type = self.sanitizer.text(event_type)
            self.seq += 1
            self.counts[event_type] += 1
            data = self.sanitizer.clean(payload or {})
            if len(json.dumps(data, ensure_ascii=False).encode()) > 12000:
                data = {"truncated": True}
            event = {
                "schema_version": 1,
                "run_id": self.run_id,
                "seq": self.seq,
                "timestamp": utc_now(),
                "event_type": event_type,
                **_labels.get(),
                "payload": data,
            }
            self._send("event", event)

    def console(self, entry):
        with self.lock:
            self._send("console", self.sanitizer.clean(entry))

    def history(self, entry):
        with self.lock:
            entry = self.sanitizer.clean(entry)
            if len(json.dumps(entry, ensure_ascii=False).encode()) > 12000:
                entry = {"metadata": entry.get("metadata"), "truncated": True}
            self._send(
                "history",
                _labels.get().get("attempt", 1),
                {"run_id": self.run_id, **_labels.get(), **entry},
            )

    @contextmanager
    def run(self, *, url=None, task=None, model=None):
        if self.active:
            yield self
            return
        self.active = True
        self.started = time.monotonic()
        started_at = utc_now()
        token = _current.set(self)
        context_token = _labels.set({})
        handler = _LogHandler(self)
        self._send("start", self.run_id)
        logging.getLogger().addHandler(handler)
        termination = "returned"
        try:
            self.metadata = self.sanitizer.clean(
                {"original_url": url, "task": task, "model": model}
            )
            self.emit("run.started", self.metadata)
            yield self
        except BaseException as exc:
            termination = (
                "interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                or type(exc).__name__ == "CancelledError"
                else "exception"
            )
            self.emit("run.failed", {"exception": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            try:
                elapsed = time.monotonic() - self.started
                self.emit("run.finished", {"termination": termination, "duration_seconds": elapsed})
                summary = {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "started_at": started_at,
                    "ended_at": utc_now(),
                    "termination": termination,
                    "exit_code": self.exit_code,
                    "metadata": self.metadata,
                    "outcome": self.sanitizer.clean(self.outcome),
                    "metrics": {
                        "duration_seconds": elapsed,
                        "events": dict(self.counts),
                        "phase_seconds": dict(self.durations),
                        "llm_calls_by_source": dict(self.llm_sources),
                        "tokens": dict(self.tokens) if self.usage_calls else None,
                        "usage_calls": self.usage_calls,
                        "usage_missing_calls": max(
                            0, self.counts["llm.started"] - self.usage_calls
                        ),
                        "steps": self.counts["step.started"],
                        "tools": self.counts["tool.started"],
                        "attempts": self.counts["attempt.started"],
                        "repairs": self.counts["repair.started"],
                        "validations": self.counts["validation.started"],
                    },
                    "trace_errors": self.errors,
                }
                self._send("summary", summary)
            finally:
                logging.getLogger().removeHandler(handler)
                _labels.reset(context_token)
                _current.reset(token)
                self.active = False


@contextmanager
def operation(name, **payload):
    """A phase or call span; always closes on errors/cancellation."""
    trace = current_trace()
    details = {"outcome": "returned"}
    if trace is None:
        yield details
        return
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    with labels(parent_call_id=_labels.get().get("call_id"), call_id=call_id):
        emit(name + ".started", **payload)
        try:
            yield details
        except BaseException as exc:
            details.update(exception=type(exc).__name__, message=str(exc), outcome="raised")
            if getattr(exc, "code", None) is not None:
                details["code"] = exc.code
            if getattr(exc, "status_code", None) is not None:
                details["status_code"] = exc.status_code
            raise
        finally:
            duration = time.monotonic() - started
            trace.durations[name] += duration
            emit(name + ".finished", **payload, **details, duration_seconds=duration)


def capture_usage(usage):
    slot = _usage.get()
    if slot is not None and usage is not None:
        slot["usage"] = {key: getattr(usage, key, None) for key in Sanitizer.NUMERIC}


def traced_llm(function):
    @functools.wraps(function)
    async def wrapped(self, messages, *args, **kwargs):
        trace = current_trace()
        if trace is None:
            return await function(self, messages, *args, **kwargs)
        source = getattr(self, "trace_source", None) or _labels.get().get("source", "main")
        if source not in {"main", "requirements", "wait"}:
            source = "other"
        with (
            labels(source=source),
            operation("llm", model=self.model, message_count=len(messages)) as details,
        ):
            trace.llm_sources[source] += 1
            token = _usage.set(details)
            try:
                return await function(self, messages, *args, **kwargs)
            finally:
                _usage.reset(token)
                usage = details.get("usage")
                if usage is not None:
                    trace.usage_calls += 1
                    trace.tokens.update({k: v for k, v in usage.items() if v is not None})

    return wrapped


def traced_task(function):
    @functools.wraps(function)
    def wrapped(url, task, settings, *args, trace=None, **kwargs):
        recorder = trace or current_trace()
        if recorder is None:
            return function(url, task, settings, *args, **kwargs)
        # Registration also protects URLs/selectors/metadata containing the key.
        key = settings.api_key.get_secret_value()
        if key and key not in recorder.sanitizer.secrets:
            recorder.sanitizer.secrets = (*recorder.sanitizer.secrets, key)
        with recorder.run(url=url, task=task, model=settings.model):
            recorder.metadata["model"] = recorder.sanitizer.clean(settings.model, "model")
            with operation("workflow"):
                outcome = function(url, task, settings, *args, **kwargs)
                recorder.outcome = outcome.summary()
                emit("workflow.result", **recorder.outcome)
                return outcome

    return wrapped


class StepObserver:
    """Per-Agent callbacks; snapshots use History metadata without page text."""

    def __init__(self):
        self.scope = None
        self.span = None
        self.details = None
        self.history_index = 0

    async def start(self, agent):
        self.scope = labels(step=agent.state.n_steps)
        self.scope.__enter__()
        self.span = operation("step")
        self.details = self.span.__enter__()

    def planned(self, state, output, step):
        emit(
            "step.planned",
            actions=[
                {"tool": next(iter(action.model_dump(exclude_unset=True)), "unknown")}
                for action in output.action
            ],
        )

    def snapshot(self, agent):
        trace = current_trace()
        if trace is None:
            return
        history = agent.history.history
        for item in history[self.history_index :]:
            entry = {
                "metadata": item.metadata.model_dump() if item.metadata else None,
                "url": item.state.url,
                "actions": [
                    action.model_dump(exclude_unset=True)
                    for action in item.model_output.action[:10]
                ]
                if item.model_output
                else [],
                "results": [
                    {
                        "error": result.error,
                        "success": result.success,
                        "is_done": result.is_done,
                        "output_chars": len(result.extracted_content or ""),
                    }
                    for result in item.result
                ],
            }
            trace.history(entry)
        self.history_index = len(history)

    async def end(self, agent):
        self.finish(agent)

    def finish(self, agent, *, interrupted=False):
        try:
            self.snapshot(agent)
            if self.details is not None:
                self.details["incomplete"] = interrupted
                self.details["errors"] = [
                    result.error for result in (agent.state.last_result or []) if result.error
                ]
        finally:
            if self.span is not None:
                self.span.__exit__(None, None, None)
                self.span = None
                self.scope.__exit__(None, None, None)
                self.scope = None
                self.details = None
