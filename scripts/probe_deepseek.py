"""Opt-in DeepSeek protocol probe; never imported or run by the test suite."""

import argparse
import asyncio
import json
import os

from openai import AsyncOpenAI


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
TOOL = {
    "type": "function",
    "function": {"name": "answer", "description": "Return the answer", "parameters": SCHEMA},
}


def summarize(response):
    choice = response.choices[0]
    message = choice.message
    return {
        "finish_reason": choice.finish_reason,
        "content": message.content,
        "tool_calls": [call.model_dump() for call in message.tool_calls or []],
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-flash")
    args = parser.parse_args()
    key = os.getenv("AUTOSCRAPER_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("Set AUTOSCRAPER_API_KEY or DEEPSEEK_API_KEY")
    client = AsyncOpenAI(api_key=key, base_url="https://api.deepseek.com/v1", max_retries=0)

    cases = [
        (
            "prompt_only",
            {"messages": [{"role": "user", "content": "Return only JSON: {\"answer\":\"ok\"}."}]},
        ),
        (
            "json_object",
            {
                "messages": [
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": "Return an object with answer set to ok."},
                ],
                "response_format": {"type": "json_object"},
            },
        ),
        (
            "json_object_after_history",
            {
                "messages": [
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": "Return an object with answer set to first."},
                    {"role": "assistant", "content": '{"answer":"first"}'},
                    {"role": "user", "content": "Now return an object with answer set to ok."},
                ],
                "response_format": {"type": "json_object"},
            },
        ),
        (
            "forced_tool",
            {
                "messages": [{"role": "user", "content": "Return answer ok using the tool."}],
                "tools": [TOOL],
                "tool_choice": {"type": "function", "function": {"name": "answer"}},
            },
        ),
        (
            "forced_tool_after_history",
            {
                "messages": [
                    {"role": "user", "content": "First inspect, then answer."},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_previous",
                                "type": "function",
                                "function": {"name": "answer", "arguments": '{"answer":"first"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_previous", "content": "done"},
                    {"role": "user", "content": "Now return answer ok."},
                ],
                "tools": [TOOL],
                "tool_choice": {"type": "function", "function": {"name": "answer"}},
            },
        ),
    ]
    for name, params in cases:
        try:
            response = await client.chat.completions.create(
                model=args.model, max_tokens=128, temperature=0, **params
            )
            result = summarize(response)
        except Exception as exc:
            result = {"error_type": type(exc).__name__, "error": str(exc)}
        print(json.dumps({"case": name, "request": list(params), "response": result}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
