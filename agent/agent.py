"""The Ballast agent: connects an AI model (Bedrock) to the Ballast MCP server.

    user text -> model -> (tool calls -> MCP server -> results -> model)* -> spoken reply

The agent never approves anything. When a tool needs the user's confirmation, the MCP server
asks through elicitation, and the agent hands that question to `confirm` (a terminal prompt,
or the demo site's confirm card).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import mcp.types as t
from mcp import Client

from .llm import LLM, ModelTurn
from .prompt import SYSTEM_PROMPT

ConfirmFn = Callable[[str], Awaitable[bool]]   # message shown to the user -> did they say yes?
MAX_STEPS = 8


@dataclass
class Reply:
    text: str
    trace: list[dict] = field(default_factory=list)   # every tool call, for the "under the hood" panel
    error: bool = False


def _short_error(e: Exception) -> str:
    """One readable line from a boto3/Bedrock error (or any error)."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict) and "Error" in resp:
        return f"{resp['Error'].get('Code', type(e).__name__)}: {resp['Error'].get('Message', '')}"
    return f"{type(e).__name__}: {e}"


def mcp_tool_to_bedrock(tool) -> dict:
    schema = tool.input_schema or {"type": "object", "properties": {}}
    return {"toolSpec": {"name": tool.name, "description": tool.description or tool.name,
                         "inputSchema": {"json": schema}}}


def _result_payload(result) -> tuple[Any, bool]:
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    try:
        return json.loads(text), bool(result.is_error)
    except (json.JSONDecodeError, TypeError):
        return {"message": text}, bool(result.is_error)


class BallastAgent:
    def __init__(self, server, llm: LLM, confirm: ConfirmFn, system_prompt: str = SYSTEM_PROMPT,
                 on_event: Callable[[dict], Awaitable[None]] | None = None):
        """server: an MCP server URL ("http://127.0.0.1:8000/mcp") or an in-process server.
        on_event: optional callback, called live for every tool call and confirmation (for UIs)."""
        self.server = server
        self.on_event = on_event
        self.llm = llm
        self.confirm = confirm
        self.system = system_prompt
        self.history: list[dict] = []
        self._turn_lock = asyncio.Lock()
        self._client: Client | None = None
        self._tools: list[dict] = []
        self.trace: list[dict] = []

    async def __aenter__(self):
        self._client = Client(self.server, elicitation_callback=self._on_elicit)
        await self._client.__aenter__()
        self._tools = [mcp_tool_to_bedrock(x) for x in (await self._client.list_tools()).tools]
        return self

    async def __aexit__(self, *exc):
        await self._client.__aexit__(*exc)

    async def _on_elicit(self, ctx, params):
        """The MCP server wants the USER to confirm something. Ask them, not the model."""
        yes = await self.confirm(params.message)
        await self._emit({"event": "user_confirmation", "message": params.message, "answer": yes})
        return t.ElicitResult(action="accept", content={"confirm": yes})

    async def _emit(self, event: dict) -> None:
        self.trace.append(event)
        if self.on_event:
            await self.on_event(event)

    async def ask(self, user_text: str) -> Reply:
        """One user turn. The turn is built separately and only added to the conversation once it
        finishes, so an interrupted or failed turn can never leave a half-written history behind."""
        async with self._turn_lock:                 # never two turns at once
            self.trace = []
            self.history = repair_history(self.history)
            turn_msgs: list[dict] = [{"role": "user", "content": [{"text": user_text}]}]

            for _ in range(MAX_STEPS):
                try:
                    turn: ModelTurn = await asyncio.to_thread(
                        self.llm.respond, self.system, self.history + turn_msgs, self._tools)
                except Exception as e:  # model unavailable, access not granted, throttled, network...
                    return Reply(f"I couldn't reach the AI model: {_short_error(e)}", self.trace, error=True)

                turn_msgs.append({"role": "assistant", "content": turn.raw_content or [{"text": turn.text}]})
                if not turn.tool_calls:
                    self.history += turn_msgs
                    return Reply(turn.text.strip(), self.trace)

                results = []
                for call in turn.tool_calls:
                    try:
                        payload, is_error = _result_payload(await self._client.call_tool(call.name, call.input))
                    except Exception as e:  # tool crashed or connection dropped: tell the model, don't die
                        payload, is_error = {"error": f"The tool failed: {_short_error(e)}"}, True
                    await self._emit({"event": "tool_call", "tool": call.name, "input": call.input,
                                      "output": payload, "error": is_error})
                    results.append({"toolResult": {
                        "toolUseId": call.id,
                        "content": [{"json": payload if isinstance(payload, dict) else {"result": payload}}],
                        "status": "error" if is_error else "success",
                    }})
                turn_msgs.append({"role": "user", "content": results})

            return Reply("Sorry, that took too many steps. Could you ask again more simply?", self.trace)


def repair_history(history: list[dict]) -> list[dict]:
    """Keep the conversation valid for Bedrock: every assistant tool request must be followed by a
    user message with a result for each one. Anything after the first broken spot is dropped."""
    for i, msg in enumerate(history):
        if msg["role"] != "assistant":
            continue
        ids = {b["toolUse"]["toolUseId"] for b in msg["content"] if "toolUse" in b}
        if not ids:
            continue
        nxt = history[i + 1] if i + 1 < len(history) else None
        got = {b["toolResult"]["toolUseId"] for b in (nxt or {}).get("content", []) if "toolResult" in b}
        if nxt is None or nxt["role"] != "user" or not ids <= got:
            # cut back to the user message that started this broken turn
            cut = i
            while cut > 0 and not (history[cut - 1]["role"] == "user"
                                   and any("text" in b for b in history[cut - 1]["content"])):
                cut -= 1
            return history[:max(cut - 1, 0)]
    return history
