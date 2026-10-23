import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from pydantic import ValidationError

from .executor import ContentTimeout, NavigationError, ScrapeError, ScraperExecutor
from .export import export_records
from .spec import ScraperSpec


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a declarative ScraperSpec")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, action="append", required=True)
    parser.add_argument("--content-timeout-ms", type=int)
    args = parser.parse_args()
    try:
        for output in args.output:
            if output.suffix.lower() not in {".csv", ".json"}:
                raise ValueError("Output extension must be .csv or .json")
            if output.resolve() == args.spec.resolve():
                raise ValueError("Output must not overwrite the input spec")
        spec = ScraperSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
        if args.content_timeout_ms is not None:
            spec = ScraperSpec.model_validate(
                {**spec.model_dump(), "content_timeout_ms": args.content_timeout_ms}
            )
        result = ScraperExecutor().run(spec)
        for output in args.output:
            export_records(result.records, output, list(spec.all_fields))
        print(
            json.dumps(
                {
                    "items": len(result.records),
                    "pages": result.pages_visited,
                    "detail_pages": result.detail_pages_visited,
                    "duplicates": result.duplicates,
                    "errors": result.errors,
                    "error_details": [vars(error) for error in result.error_details],
                    "stop_reason": result.stop_reason,
                },
                ensure_ascii=False,
            )
        )
        return 2 if result.errors or result.stop_reason == "detail_limit" else 0
    except (OSError, ValueError, ValidationError, ScrapeError, PlaywrightError) as exc:
        code = (
            exc.code
            if isinstance(exc, (NavigationError, ContentTimeout))
            else "selector_failure"
            if isinstance(exc, ScrapeError)
            else "execution_failure"
        )
        print(json.dumps({"code": code, "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
