"""Generate a reusable rule; optionally hand it to the existing executor."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import AgentSettings, generate_spec, save_spec
from .executor import ScraperExecutor
from .export import export_records


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
    parser.add_argument(
        "--structured-output",
        action="store_true",
        help="Use provider json_schema mode (off for compatibility by default)",
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
        settings = AgentSettings.from_env(
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
        # Finish the async explorer before calling the executor's synchronous Playwright API.
        spec = asyncio.run(generate_spec(args.url, args.task, settings))
        save_spec(spec, args.spec_output)
        summary = {"spec": str(args.spec_output)}
        if args.output:
            result = ScraperExecutor().run(spec)
            for output in args.output:
                export_records(result.records, output, list(spec.fields))
            summary.update(
                items=len(result.records),
                pages=result.pages_visited,
                errors=result.errors,
                stop_reason=result.stop_reason,
            )
            print(json.dumps(summary, ensure_ascii=False))
            return 2 if result.errors else 0
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
