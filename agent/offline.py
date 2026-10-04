"""A tiny keyword-based stand-in for the AI model.

Used for offline UI work and as a demo backup if Bedrock is unreachable.
It follows the same rules as the real agent: tools for every number, plan before trading,
and the user confirms trades directly. Set BALLAST_LLM=offline to use it.
"""
from __future__ import annotations

import re

from .llm import ModelTurn, _parse

YES = re.compile(r"\b(yes|yeah|yep|sure|go ahead|do it|confirm|okay|ok)\b")
NO = re.compile(r"\b(no|nope|cancel|don't|do not|stop)\b")


def _round(p: str) -> str:
    return f"{round(float(p.rstrip('%')))} percent"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _label(cat: str) -> str:
    return {"us_stocks": "US stocks", "intl_stocks": "international stocks"}.get(cat, cat.replace("_", " "))


class OfflineLLM:
    model_id = "offline-rules"

    def __init__(self):
        self._n = 0

    def _tool(self, name: str, args: dict | None = None) -> ModelTurn:
        self._n += 1
        return _parse([{"toolUse": {"toolUseId": f"off-{self._n}", "name": name, "input": args or {}}}])

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        last = messages[-1]["content"][0]
        if "toolResult" in last:
            name = self._tool_name(messages, last["toolResult"]["toolUseId"])
            return _parse([{"text": self._describe(name, last["toolResult"]["content"][0]["json"])}])

        text = last.get("text", "").lower()
        if re.search(r"\b(invest|recommend|should i buy|best stock|pick)\b", text):
            return _parse([{"text": "I don't recommend investments. I keep your portfolio on the targets "
                                    "you set. I can show your targets or help you change them."}])
        if NO.search(text) and not YES.search(text):
            return _parse([{"text": "Okay, nothing was traded."}])
        if YES.search(text):
            pid = self._last_proposal(messages)
            if pid:
                return self._tool("execute_rebalance", {"proposal_id": pid})
        if re.search(r"\b(rebalance|fix|balance|back on target)\b", text):
            return self._tool("plan_rebalance")
        if re.search(r"\b(done|activity|history|today|log)\b", text):
            return self._tool("get_activity")
        if re.search(r"\b(rules?|targets?|limits?)\b", text):
            return self._tool("get_rules")
        return self._tool("get_portfolio")

    # ---------- helpers ----------

    @staticmethod
    def _tool_name(messages, tool_use_id) -> str:
        for m in reversed(messages):
            for b in m["content"]:
                if "toolUse" in b and b["toolUse"]["toolUseId"] == tool_use_id:
                    return b["toolUse"]["name"]
        return ""

    @staticmethod
    def _last_proposal(messages) -> str | None:
        for m in reversed(messages):
            for b in m["content"]:
                if "toolResult" in b:
                    out = b["toolResult"]["content"][0]["json"]
                    if out.get("status") == "awaiting_approval":
                        return out.get("proposal_id")
        return None

    @staticmethod
    def _describe(name: str, out: dict) -> str:
        status = out.get("status")
        if name == "get_portfolio":
            if status != "ok":
                return out.get("message", "I couldn't read your portfolio.")
            worst = max(out.get("drift", []), key=lambda r: abs(float(r["drift_points"])), default=None)
            total = f"${float(out['total_value']):,.0f}"
            if not out.get("needs_rebalance") or worst is None:
                return f"Your paper portfolio is worth {total} and everything is within your drift limit."
            return (f"Your paper portfolio is worth {total}. {_cap(_label(worst['category']))} are at "
                    f"{_round(worst['current'])}, {abs(round(float(worst['drift_points'])))} points off your "
                    f"{_round(worst['target'])} target, which is past your limit. Want me to plan a rebalance?")
        if name == "plan_rebalance":
            if status == "awaiting_approval":
                parts = [f"{t['side']} ${float(t['amount']):,.0f} of {t['symbol']}" for t in out["trades"]]
                return f"Here's the plan: {' and '.join(parts)}. Nothing is placed yet. Want me to go ahead?"
            if status == "blocked":
                return "I can't plan that rebalance. " + " ".join(out.get("reasons", []))
            return out.get("message", "You're on target. No trades needed.")
        if name == "execute_rebalance":
            return {
                "executed": "Done. The paper orders filled and you're back on your targets.",
                "cancelled": "Okay, I didn't place anything.",
                "market_closed": "The market is closed, so I didn't place anything.",
            }.get(status, out.get("message") or " ".join(out.get("errors", [])) or "That didn't go through.")
        if name == "get_activity":
            filled = sum(1 for e in out.get("events", []) if e["event"] == "orders_filled")
            made = sum(1 for e in out.get("events", []) if e["event"] == "proposal_created")
            return f"I've made {made} plan{'s' if made != 1 else ''} and completed {filled} rebalance{'s' if filled != 1 else ''}."
        if name == "get_rules":
            t = out.get("targets", {})
            return "Your targets are " + ", ".join(f"{_round(v)} {_label(k)}" for k, v in t.items()) + "."
        return "Done."
