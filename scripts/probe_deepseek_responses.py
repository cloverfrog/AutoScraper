"""Opt-in Responses API probe. Not collected by pytest or run by default."""

import argparse
import asyncio
import json
import os

from openai import AsyncOpenAI


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--agent-output", action="store_true")
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--flash", action="store_true")
    args = parser.parse_args()
    key = os.getenv("AUTOSCRAPER_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("Set AUTOSCRAPER_API_KEY or DEEPSEEK_API_KEY")
    client = AsyncOpenAI(api_key=key, base_url="https://api.deepseek.com", max_retries=0)
    if args.agent_output:
        from browser_use.agent.views import AgentOutput
        from browser_use.llm.schema import SchemaOptimizer

        from autoscraper.agent_tools import build_tools

        actions = build_tools().registry.create_action_model()
        if args.flash:
            output_format = AgentOutput.type_with_custom_actions_flash_mode(actions)
        elif args.no_thinking:
            output_format = AgentOutput.type_with_custom_actions_no_thinking(actions)
        else:
            output_format = AgentOutput.type_with_custom_actions(actions)
        schema = SchemaOptimizer.create_optimized_json_schema(output_format)
        prompt = "Inspect the current page using inspect_page with an empty root_selector."
    else:
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        prompt = "Return answer ok."
    response = await client.responses.create(
        model=args.model,
        input=prompt,
        max_output_tokens=1024,
        text={"format": {"type": "json_schema", "name": "probe", "schema": schema}},
    )
    print(
        json.dumps(
            {
                "status": response.status,
                "schema_chars": len(json.dumps(schema)),
                "output_text": response.output_text,
                "output": [item.model_dump() for item in response.output],
                "error": response.error.model_dump() if response.error else None,
                "incomplete_details": (
                    response.incomplete_details.model_dump()
                    if response.incomplete_details
                    else None
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
