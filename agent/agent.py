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
    def __init__(self, server, llm: LLM, confirm: ConfirmFn, system_prompt: str = SYSTEM_PROMPT):
        """server: an MCP server URL ("http://127.0.0.1:8000/mcp") or an in-process server."""
        self.server = server
        self.llm = llm
        self.confirm = confirm
        self.system = system_prompt
        self.history: list[dict] = []
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
        self.trace.append({"event": "user_confirmation", "message": params.message, "answer": yes})
        return t.ElicitResult(action="accept", content={"confirm": yes})

    async def ask(self, user_text: str) -> Reply:
        self.trace = []
        self.history.append({"role": "user", "content": [{"text": user_text}]})

        start = len(self.history) - 1
        for _ in range(MAX_STEPS):
            try:
                turn: ModelTurn = await asyncio.to_thread(self.llm.respond, self.system, self.history, self._tools)
            except Exception as e:  # model unavailable, access not granted, throttled, network...
                del self.history[start:]          # forget the failed turn so the next one starts clean
                return Reply(f"I couldn't reach the AI model: {_short_error(e)}", self.trace, error=True)
            self.history.append({"role": "assistant", "content": turn.raw_content or [{"text": turn.text}]})
            if not turn.tool_calls:
                return Reply(turn.text.strip(), self.trace)

            results = []
            for call in turn.tool_calls:
                result = await self._client.call_tool(call.name, call.input)
                payload, is_error = _result_payload(result)
                self.trace.append({"event": "tool_call", "tool": call.name, "input": call.input,
                                   "output": payload, "error": is_error})
                results.append({"toolResult": {
                    "toolUseId": call.id,
                    "content": [{"json": payload if isinstance(payload, dict) else {"result": payload}}],
                    "status": "error" if is_error else "success",
                }})
            self.history.append({"role": "user", "content": results})

        return Reply("Sorry, that took too many steps. Could you ask again more simply?", self.trace)
