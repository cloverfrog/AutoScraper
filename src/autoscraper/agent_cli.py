"""Generate a reusable rule; optionally hand it to the existing executor."""

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import AgentError, AgentSettings, save_spec
from .export import export_records
from .validation import ValidationSettings
from .workflow import run_task


def settings_from_env(**overrides) -> AgentSettings:
    """Apply CLI provider defaults without coupling the agent runtime to them."""
    key = os.getenv("AUTOSCRAPER_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise AgentError("Set AUTOSCRAPER_API_KEY or DEEPSEEK_API_KEY")
    data = {
        "api_key": key,
        "model": os.getenv("AUTOSCRAPER_MODEL", "deepseek-flash"),
        "base_url": os.getenv("AUTOSCRAPER_BASE_URL", "https://api.deepseek.com"),
        "executable_path": os.getenv("AUTOSCRAPER_BROWSER_PATH"),
    }
    data.update({key: value for key, value in overrides.items() if value is not None})
    return AgentSettings.model_validate(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Explore a website and generate a ScraperSpec")
    parser.add_argument("url")
    parser.add_argument("task")
    parser.add_argument("--spec-output", required=True, type=Path)
    parser.add_argument("--output", type=Path, action="append", default=[])
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--timeout", type=int, dest="timeout_seconds")
    parser.add_argument("--llm-timeout", type=int, dest="llm_timeout_seconds")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--browser-path", dest="executable_path")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--fields", nargs="+", help="Authoritative required field names")
    parser.add_argument("--count", type=int, help="Authoritative requested record count")
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--validation-items", type=int, default=100)
    parser.add_argument("--validation-pages", type=int, default=5)
    parser.add_argument("--validation-timeout-ms", type=int, default=5000)
    parser.add_argument(
        "--structured-output",
        action="store_true",
        help="Legacy compatibility flag; Responses output always uses json_schema",
    )
    args = parser.parse_args()
    try:
        paths = [args.spec_output.resolve(), *(p.resolve() for p in args.output)]
        if len(paths) != len(set(paths)):
            raise ValueError("Rule and data output paths must be distinct")
        if args.spec_output.suffix.lower() != ".json":
            raise ValueError("Rule output must be .json")
        if any(p.suffix.lower() not in {".json", ".csv"} for p in args.output):
            raise ValueError("Data output must be .json or .csv")
        settings = settings_from_env(
            **{
                name: getattr(args, name)
                for name in (
                    "model",
                    "base_url",
                    "max_steps",
                    "timeout_seconds",
                    "llm_timeout_seconds",
                    "max_output_tokens",
                    "executable_path",
                )
            },
            headless=not args.headed,
            structured_output=args.structured_output,
        )
        validation = ValidationSettings(
            max_repairs=args.max_repairs,
            max_items=args.validation_items,
            max_pages=args.validation_pages,
            timeout_ms=args.validation_timeout_ms,
        )
        outcome = run_task(
            args.url,
            args.task,
            settings,
            validation=validation,
            fields=args.fields,
            count=args.count,
            execute=bool(args.output),
        )
        summary = outcome.summary()
        if outcome.status not in {"success", "validated"}:
            print(json.dumps(summary, ensure_ascii=False))
            return 3 if outcome.status == "unsupported" else 1
        # No rule/data writes until constraints and execution/data checks pass.
        save_spec(outcome.spec, args.spec_output)
        if args.output:
            for output in args.output:
                export_records(outcome.data.records, output, list(outcome.spec.fields))
        summary["spec"] = str(args.spec_output)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "unfulfilled",
                    "task_fulfilled": False,
                    "feedback": [{"code": "workflow_failure", "message": str(exc)}],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
