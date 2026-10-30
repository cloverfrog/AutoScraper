# 两个 CLI 共用的人工接管入口：只接收继续/取消，不接收或记录浏览器登录凭据。
"""Shared manual browser control for both command-line entry points."""

import sys

from autoscraper.browser.access import HandoffRequest


# 提示写入 stderr，保持 stdout 的结果 JSON 可解析；非交互输入不消费管道内容。
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
