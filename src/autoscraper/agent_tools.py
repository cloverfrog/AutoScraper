"""Fixed, read-only DOM helpers; no model-controlled executable source."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlparse

from browser_use import ActionResult, BrowserSession, ChatOpenAI, Tools


# JavaScript below is authored by the application. Model inputs are JSON arguments,
# never interpolated source. No evaluate/code/file tools are exposed to the agent.
INSPECT_SCRIPT = """(selector) => {
    const root = selector ? document.querySelector(selector) : document.body;
    if (!root) return {error: 'No root matched'};
    const attrs = el => Object.fromEntries([...el.attributes]
        .filter(a => ['id','class','href','src','title','aria-label','aria-disabled'].includes(a.name))
        .map(a => [a.name, a.value.slice(0, 250)]));
    const nodes = [root, ...root.querySelectorAll('*')]
        .filter(el => !['SCRIPT','STYLE','NOSCRIPT','SVG','PATH'].includes(el.tagName));
    return {url: location.href, title: document.title, total_nodes: nodes.length,
        nodes: nodes.slice(0, 180).map(el => ({tag: el.tagName.toLowerCase(),
            attrs: attrs(el), children: el.children.length,
            text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120)}))};
}"""

SELECTOR_SCRIPT = """(itemSelector, fieldSelector, attribute) => {
    const items = [...document.querySelectorAll(itemSelector)];
    return {url: location.href, count: items.length,
        samples: items.slice(0, 5).map(item => {
            const matches = fieldSelector ? [...item.querySelectorAll(fieldSelector)] : [item];
            return {matches: matches.length, values: matches.slice(0, 2).map(el =>
                attribute ? el.getAttribute(attribute)?.slice(0, 300) ?? null
                          : (el.innerText || '').trim().slice(0, 300))};
        })};
}"""


def build_tools() -> Tools:
    tools = Tools()
    for name in list(tools.registry.registry.actions):
        if name not in {"navigate", "done"}:
            tools.registry.exclude_action(name)

    @tools.action(
        description="Inspect DOM tags, classes and attributes. Optional CSS root narrows output."
    )
    async def inspect_page(
        browser_session: BrowserSession, root_selector: str = ""
    ) -> ActionResult:
        page = await browser_session.must_get_current_page()
        content = await page.evaluate(INSPECT_SCRIPT, root_selector)
        return ActionResult(extracted_content=content[:30000])

    @tools.action(
        description="Test CSS item selector and optional field selector relative to each item. "
        "Return counts and 5 samples; attribute is optional. Also test Next href this way."
    )
    async def test_selector(
        browser_session: BrowserSession,
        item_selector: str,
        field_selector: str = "",
        attribute: str = "",
    ) -> ActionResult:
        page = await browser_session.must_get_current_page()
        return ActionResult(
            extracted_content=await page.evaluate(
                SELECTOR_SCRIPT, item_selector, field_selector, attribute
            )
        )

    return tools


@dataclass
class LimitedChatOpenAI(ChatOpenAI):
    """Hard call cap even if browser-use internally retries model-output parsing."""

    max_calls: int = 12
    calls: int = field(default=0, init=False)

    async def ainvoke(self, messages, output_format=None, **kwargs):
        if self.calls >= self.max_calls:
            raise RuntimeError("LLM call budget exhausted")
        self.calls += 1
        return await super().ainvoke(messages, output_format=output_format, **kwargs)


class _JSONModeCompletions:
    """Translate OpenAI JSON Schema requests to DeepSeek's supported JSON mode."""

    def __init__(self, completions):
        self._completions = completions

    async def create(self, *args, **kwargs):
        if "response_format" in kwargs:
            kwargs["response_format"] = {"type": "json_object"}
        return await self._completions.create(*args, **kwargs)


@dataclass
class LimitedDeepSeekChatOpenAI(LimitedChatOpenAI):
    """Use DeepSeek JSON mode for browser-use AgentOutput, without function tools."""

    def get_client(self):
        client = super().get_client()
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=_JSONModeCompletions(client.chat.completions),
            )
        )


def make_llm(settings) -> LimitedChatOpenAI:
    hostname = (urlparse(str(settings.base_url)).hostname or "").lower()
    is_official_deepseek = hostname == "api.deepseek.com" and settings.model.startswith(
        "deepseek-"
    )
    llm_type = LimitedDeepSeekChatOpenAI if is_official_deepseek else LimitedChatOpenAI
    return llm_type(
        model=settings.model,
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url),
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        max_calls=settings.max_steps,
        max_completion_tokens=settings.max_output_tokens,
        temperature=0,
        frequency_penalty=None,
        add_schema_to_system_prompt=True,
        # deepseek-flash may emit its private DSML tool syntax as text when JSON is
        # prompt-only. Official JSON mode keeps AgentOutput in message.content.
        dont_force_structured_output=False
        if is_official_deepseek
        else not settings.structured_output,
    )
