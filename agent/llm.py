"""The AI model behind the agent, behind one small interface.

BedrockLLM talks to a real model on Amazon Bedrock (Converse API).
ScriptedLLM replays canned replies, so the agent can be tested without AWS.
Both speak the Bedrock Converse message format.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelTurn:
    """One reply from the model: some text, and/or requests to call tools."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_content: list[dict] = field(default_factory=list)  # exact content blocks, kept for history


class LLM(Protocol):
    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


def _parse(content: list[dict]) -> ModelTurn:
    turn = ModelTurn(raw_content=content)
    for block in content:
        if "text" in block:
            turn.text += block["text"]
        elif "toolUse" in block:
            tu = block["toolUse"]
            turn.tool_calls.append(ToolCall(tu["toolUseId"], tu["name"], tu.get("input") or {}))
    return turn


class BedrockLLM:
    def __init__(self, model_id: str | None = None, region: str | None = None,
                 max_tokens: int = 700, temperature: float = 0.2, client=None):
        import boto3

        self.model_id = model_id or os.environ.get("BALLAST_MODEL", DEFAULT_MODEL)
        self.client = client or boto3.client(
            "bedrock-runtime", region_name=region or os.environ.get("AWS_REGION", "us-east-1"))
        self.inference = {"maxTokens": max_tokens, "temperature": temperature}

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        kwargs = {"modelId": self.model_id, "system": [{"text": system}],
                  "messages": messages, "inferenceConfig": self.inference}
        if tools:
            kwargs["toolConfig"] = {"tools": tools}
        r = self.client.converse(**kwargs)
        return _parse(r["output"]["message"]["content"])


class ScriptedLLM:
    """Replays a fixed list of replies. Each reply is a string (final answer), a list of
    (tool_name, input) pairs (tool calls), or a function(messages) returning one of those.
    Records what it was sent, for assertions."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        step = self.script.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return _parse([{"text": step}])
        blocks = [{"toolUse": {"toolUseId": f"t{len(self.calls)}-{i}", "name": n, "input": a}}
                  for i, (n, a) in enumerate(step)]
        return _parse(blocks)


def last_tool_output(messages: list[dict]) -> dict:
    """Helper for scripts: the JSON result of the most recent tool call in the conversation."""
    for m in reversed(messages):
        for block in m["content"]:
            if "toolResult" in block:
                return block["toolResult"]["content"][0]["json"]
    return {}
