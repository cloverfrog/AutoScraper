"""Shared manual browser control for both command-line entry points."""

import sys

from autoscraper.browser.access import HandoffRequest


def wait_for_user(request: HandoffRequest) -> str | None:
    if not sys.stdin.isatty():
        return None
    print(f"\n需要在浏览器中操作：{request.reason}", file=sys.stderr)
    print(
        "请在浏览器完成操作，回到终端按 Enter 继续；输入 q 取消。"
        "请勿在终端输入账号、密码或验证码。",
        file=sys.stderr,
        flush=True,
    )
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        return "cancel"
    except EOFError:
        return None
    if answer == "":
        return None
    return "continue" if not answer.strip() else "cancel"
