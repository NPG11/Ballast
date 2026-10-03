"""Agent tests with a scripted model (no AWS) and the real MCP server in-process (fake broker)."""
import asyncio

from adapters.fake import FakeBroker
from agent.agent import BallastAgent
from agent.llm import ScriptedLLM, last_tool_output
from mcp_server.server import build_server
from service.ballast import Ballast


def make():
    broker = FakeBroker({"VTI": ("30", "280"), "BND": ("25", "80")}, cash="2000")
    app = Ballast(broker, fill_timeout=0, sleep=lambda s: None)
    return broker, build_server(app, user_id="demo")


def user_answers(*answers):
    asked = []

    async def confirm(message):
        asked.append(message)
        return answers[len(asked) - 1]
    confirm.asked = asked
    return confirm


def run(coro):
    return asyncio.run(coro)


def test_model_sees_all_tools_in_bedrock_format():
    llm = ScriptedLLM(["Hi."])

    async def go():
        _, server = make()
        async with BallastAgent(server, llm, user_answers()) as a:
            await a.ask("hello")
    run(go())
    names = {t["toolSpec"]["name"] for t in llm.calls[0]["tools"]}
    assert {"get_portfolio", "plan_rebalance", "execute_rebalance"} <= names
    assert all("json" in t["toolSpec"]["inputSchema"] for t in llm.calls[0]["tools"])


def test_tool_results_are_sent_back_to_the_model():
    llm = ScriptedLLM([[("get_portfolio", {})], "You're at 68% stocks, 8 points over target."])

    async def go():
        _, server = make()
        async with BallastAgent(server, llm, user_answers()) as a:
            return await a.ask("How's my portfolio?")
    reply = run(go())
    assert reply.text.startswith("You're at 68%")
    assert reply.trace[0]["tool"] == "get_portfolio"
    tool_result = llm.calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert tool_result["status"] == "success"
    assert tool_result["content"][0]["json"]["allocation"]["us_stocks"] == "67.7%"


def _execute_last_proposal(messages):
    """What a sensible model does after the user says yes: execute the proposal it was shown."""
    for m in reversed(messages):
        for block in m["content"]:
            if "toolResult" in block and "proposal_id" in block["toolResult"]["content"][0]["json"]:
                pid = block["toolResult"]["content"][0]["json"]["proposal_id"]
                return [("execute_rebalance", {"proposal_id": pid})]
    raise AssertionError("no proposal in the conversation")


def test_rebalance_runs_only_after_the_user_confirms():
    confirm = user_answers(True)
    llm = ScriptedLLM([
        [("plan_rebalance", {})],
        "I'd sell $960 of VTI and buy $480 of BND. Want me to go ahead?",
        _execute_last_proposal,
        "Done. Both paper orders filled and you're back at 60, 20, 20.",
    ])

    async def go():
        broker, server = make()
        async with BallastAgent(server, llm, confirm) as a:
            first = await a.ask("Rebalance me")
            orders_after_first = dict(broker.orders)
            second = await a.ask("Yes, do it")
            return broker, orders_after_first, first, second
    broker, orders_after_first, first, second = run(go())
    assert "Want me to go ahead" in first.text and orders_after_first == {}
    assert "SELL $960.00 VTI" in confirm.asked[0]
    assert [s["event"] for s in second.trace] == ["user_confirmation", "tool_call"]
    assert second.trace[1]["output"]["status"] == "executed"
    assert len(broker.orders) == 2


def test_user_declining_means_no_trades_even_if_model_wants_them():
    llm = ScriptedLLM([[("plan_rebalance", {})], _execute_last_proposal,
                       lambda msgs: f"Okay. Status: {last_tool_output(msgs)['status']}."])

    async def go():
        broker, server = make()
        async with BallastAgent(server, llm, user_answers(False)) as a:
            return broker, await a.ask("rebalance and just do it")
    broker, reply = run(go())
    assert broker.orders == {}
    assert reply.text == "Okay. Status: cancelled."


def test_tool_errors_are_reported_to_the_model():
    llm = ScriptedLLM([[("execute_rebalance", {"proposal_id": "BL-NOPE"})], "That proposal doesn't exist."])

    async def go():
        _, server = make()
        async with BallastAgent(server, llm, user_answers()) as a:
            return await a.ask("execute BL-NOPE")
    reply = run(go())
    assert reply.trace[0]["error"] is True
    assert llm.calls[1]["messages"][-1]["content"][0]["toolResult"]["status"] == "error"


def test_conversation_history_carries_across_turns():
    llm = ScriptedLLM(["First answer.", "Second answer."])

    async def go():
        _, server = make()
        async with BallastAgent(server, llm, user_answers()) as a:
            await a.ask("one")
            await a.ask("two")
    run(go())
    texts = [m["content"][0].get("text") for m in llm.calls[1]["messages"]]
    assert texts == ["one", "First answer.", "two"]


def test_runaway_tool_loops_are_stopped():
    llm = ScriptedLLM([[("get_rules", {})]] * 8)

    async def go():
        _, server = make()
        async with BallastAgent(server, llm, user_answers()) as a:
            return await a.ask("loop forever")
    assert "too many steps" in run(go()).text


class FakeBedrockClient:
    """Stands in for boto3's bedrock-runtime client: records the request, returns a Converse reply."""

    def __init__(self, content):
        self.content, self.requests = content, []

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        return {"output": {"message": {"role": "assistant", "content": self.content}},
                "stopReason": "tool_use"}


def test_bedrock_request_and_tool_use_parsing():
    from agent.llm import BedrockLLM
    client = FakeBedrockClient([
        {"text": "Let me check."},
        {"toolUse": {"toolUseId": "abc", "name": "get_portfolio", "input": {}}},
    ])
    llm = BedrockLLM(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", client=client)
    tools = [{"toolSpec": {"name": "get_portfolio", "description": "d",
                           "inputSchema": {"json": {"type": "object", "properties": {}}}}}]
    turn = llm.respond("be brief", [{"role": "user", "content": [{"text": "hi"}]}], tools)

    req = client.requests[0]
    assert req["modelId"].startswith("us.anthropic.claude-haiku")
    assert req["system"] == [{"text": "be brief"}]
    assert req["toolConfig"] == {"tools": tools}
    assert turn.text == "Let me check."
    assert [(c.id, c.name) for c in turn.tool_calls] == [("abc", "get_portfolio")]


class BrokenLLM:
    """A model that fails like Bedrock does when access isn't granted."""

    def respond(self, system, messages, tools):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                     "Message": "Model use case details have not been submitted."}}, "Converse")


def test_model_errors_do_not_crash_the_chat():
    async def go():
        _, server = make()
        async with BallastAgent(server, BrokenLLM(), user_answers()) as a:
            first = await a.ask("How's my portfolio?")
            second = await a.ask("Still there?")
            return a, first, second
    agent, first, second = run(go())
    assert first.error and "ResourceNotFoundException" in first.text
    assert second.error                         # still answering, not crashed
    assert agent.history == []                  # failed turns don't pollute the conversation
