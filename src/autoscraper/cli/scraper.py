# 已有规则的命令行入口：autoscraper <规则.json> --output <数据文件>。
# 负责参数和路径检查、执行规则、导出数据及返回退出码，不生成规则，也不调用模型。
# 退出码：0 正常结束；2 有单条错误或详情预算耗尽；1 输入、文件或执行异常。
import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from pydantic import ValidationError

from autoscraper.exceptions import ContentTimeout, NavigationError, ScrapeError
from autoscraper.scraping.executor import ScraperExecutor
from autoscraper.scraping.export import export_records
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.browser.access import BrowserAccess
from autoscraper.exceptions import HandoffRequired
from autoscraper.cli.interaction import wait_for_user


# 校验输入与输出路径，应用可选超时覆盖，执行并导出；异常转换为 JSON 诊断和退出码。
def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a declarative ScraperSpec")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, action="append", required=True)
    parser.add_argument("--content-timeout-ms", type=int)
    parser.add_argument("--navigation-timeout-ms", type=int)
    parser.add_argument("--headed", action="store_true", help="Show the scraping browser")
    parser.add_argument("--auth-state", type=Path,
                        help="Load/save private authentication state for this site/account")
    parser.add_argument("--login", action="store_true",
                        help="Pause for manual login before scraping (requires --headed)")
    parser.add_argument(
        "--browser-path", dest="executable_path", default=os.getenv("AUTOSCRAPER_BROWSER_PATH"),
        help="Chromium / Chrome executable path (default: AUTOSCRAPER_BROWSER_PATH)",
    )
    args = parser.parse_args()
    try:
        if args.login and not args.headed:
            raise ValueError("--login requires --headed")
        if args.auth_state is not None and args.auth_state.resolve() in {
            args.spec.resolve(), *(output.resolve() for output in args.output)
        }:
            raise ValueError("Authentication state must not share a rule or data path")
        for output in args.output:
            if output.suffix.lower() not in {".csv", ".json"}:
                raise ValueError("Output extension must be .csv or .json")
            if output.resolve() == args.spec.resolve():
                raise ValueError("Output must not overwrite the input spec")
        spec = ScraperSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
        overrides = {}
        if args.content_timeout_ms is not None:
            overrides["content_timeout_ms"] = args.content_timeout_ms
        if args.navigation_timeout_ms is not None:
            overrides["timeout_ms"] = args.navigation_timeout_ms
        if overrides:
            spec = ScraperSpec.model_validate({**spec.model_dump(), **overrides})
        access = BrowserAccess(handler=wait_for_user if args.headed else None)
        if args.auth_state is not None and args.auth_state.exists():
            access.load(args.auth_state)
        result = ScraperExecutor(
            headless=not args.headed, executable_path=args.executable_path,
            access=access, login=args.login,
        ).run(spec)
        if args.auth_state is not None:
            access.save(args.auth_state)
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
    except HandoffRequired as exc:
        print(json.dumps({"status": "cancelled" if exc.cancelled else "needs_user",
                          "pending_handoff": exc.request.model_dump()}, ensure_ascii=False))
        return 130 if exc.cancelled else 5
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
