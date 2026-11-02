# Benchmark 命令入口；默认配置仅包含两个无模型验证任务。
import argparse
import sys
import uuid
from pathlib import Path

from .common import HOME, now, read_json, tasks, write_json
from .scoring import score_batch


def main():
    parser = argparse.ArgumentParser(
        description="AutoScraper lightweight benchmark (no LLM by default)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Check source availability and frozen truth drift")
    check.add_argument("--tasks", nargs="+", choices=list(tasks()), default=list(tasks()))
    check.add_argument("--output-root", type=Path, default=Path("output/benchmark-checks"))
    check.add_argument("--browser-path")
    audit = commands.add_parser("audit", help="Verify all frozen source evidence offline")
    audit.add_argument("--output", type=Path, default=Path("output/benchmark-audit.json"))
    run = commands.add_parser("run", help="Run an isolated batch")
    run.add_argument("--config", type=Path, default=HOME / "configs/smoke.json")
    run.add_argument("--commit")
    run.add_argument("--output-root", type=Path, default=Path("output/benchmark"))
    run.add_argument("--allow-llm", action="store_true")
    run.add_argument(
        "--allow-dirty", action="store_true", help="Development only; mark results non-formal"
    )
    score = commands.add_parser("score", help="Re-score a batch offline")
    score.add_argument("batch", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "audit":
            from .evidence import audit_all

            results = audit_all()
            write_json(args.output, results)
            for result in results:
                print(
                    f"{result['task']}: source_consistent={result['source_consistent']}, human_review_required={result['human_review_required']}"
                )
            return 0 if all(result["source_consistent"] for result in results) else 1
        if args.command == "score":
            summary = score_batch(args.batch)
            print(f"{args.batch / 'summary.md'} (complete={summary['complete']})")
            return 0
        if args.command == "check":
            from .runner import check_task

            directory = args.output_root.resolve() / (
                now().replace(":", "-") + "_" + uuid.uuid4().hex[:10]
            )
            results = [
                check_task(tasks()[task_id], directory / task_id, browser_path=args.browser_path)
                for task_id in args.tasks
            ]
            write_json(directory / "checks.json", results)
            print(directory)
            return 0 if all(result["passed"] for result in results) else 1
        from .runner import run_batch

        directory = run_batch(
            read_json(args.config),
            args.output_root,
            commit=args.commit,
            allow_llm=args.allow_llm,
            allow_dirty=args.allow_dirty,
        )
        print(directory)
        summary = read_json(directory / "summary.json")
        return (
            0 if summary["complete"] and all(row["task_success"] for row in summary["runs"]) else 1
        )
    except KeyboardInterrupt:
        print("Benchmark interrupted; no further runs started", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Benchmark: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
