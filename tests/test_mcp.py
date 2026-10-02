"""MCP server tests: an in-process MCP client talks to the real server (fake broker)."""
import asyncio
import json

import pytest

import mcp.types as t
from mcp import Client
from mcp.shared.exceptions import MCPError

from adapters.fake import FakeBroker
from mcp_server.server import build_server
from service.ballast import Ballast


def make():
    broker = FakeBroker({"VTI": ("30", "280"), "BND": ("25", "80")}, cash="2000")
    app = Ballast(broker, fill_timeout=0, sleep=lambda s: None)
    return broker, app, build_server(app, user_id="demo")


def user_says(answer: bool):
    async def cb(ctx, params):
        cb.asked.append(params.message)
        return t.ElicitResult(action="accept", content={"confirm": answer})
    cb.asked = []
    return cb


async def call(client, name, args=None):
    r = await client.call_tool(name, args or {})
    return json.loads(r.content[0].text)


def run(coro):
    return asyncio.run(coro)


def test_tools_are_listed():
    async def go():
        _, _, server = make()
        async with Client(server) as c:
            return {x.name for x in (await c.list_tools()).tools}
    assert run(go()) == {"get_portfolio", "get_rules", "list_categories", "get_order_status",
                         "get_activity", "plan_rebalance", "execute_rebalance",
                         "preview_target_allocation", "save_target_allocation"}


def test_ai_cannot_see_or_fill_the_approval():
    async def go():
        _, _, server = make()
        async with Client(server) as c:
            tools = {x.name: x for x in (await c.list_tools()).tools}
            return tools["execute_rebalance"].input_schema["properties"]
    assert list(run(go())) == ["proposal_id"]


def test_full_rebalance_with_user_confirmation():
    yes = user_says(True)

    async def go():
        broker, app, server = make()
        async with Client(server, elicitation_callback=yes) as c:
            status = await call(c, "get_portfolio")
            plan = await call(c, "plan_rebalance")
            done = await call(c, "execute_rebalance", {"proposal_id": plan["proposal_id"]})
            log = await call(c, "get_activity")
            return status, plan, done, log
    status, plan, done, log = run(go())
    assert status["needs_rebalance"]
    assert plan["status"] == "awaiting_approval"
    assert "SELL $960.00 VTI" in yes.asked[0] and "BUY $480.00 BND" in yes.asked[0]
    assert done["status"] == "executed" and done["still_needs_rebalance"] is False
    assert [e["event"] for e in log["events"]] == ["proposal_created", "proposal_approved", "orders_filled"]


def test_declined_rules_change_saves_nothing():
    async def go():
        _, _, server = make()
        async with Client(server, elicitation_callback=user_says(False)) as c:
            res = await call(c, "save_target_allocation",
                             {"targets_percent": {"us_stocks": 50, "bonds": 30, "cash": 20}})
            return res, await call(c, "get_rules")
    res, rules = run(go())
    assert res["status"] == "cancelled" and rules["targets"]["us_stocks"] == "60.0%"


def test_user_declines_nothing_traded():
    async def go():
        broker, _, server = make()
        async with Client(server, elicitation_callback=user_says(False)) as c:
            plan = await call(c, "plan_rebalance")
            res = await call(c, "execute_rebalance", {"proposal_id": plan["proposal_id"]})
            return broker, res
    broker, res = run(go())
    assert res["status"] == "cancelled" and broker.orders == {}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_client_that_cannot_ask_the_user_cannot_trade(mode):
    async def go():
        broker, _, server = make()
        async with Client(server, mode=mode) as c:          # no elicitation support
            plan = await call(c, "plan_rebalance")
            with pytest.raises(MCPError, match="elicitation"):
                await c.call_tool("execute_rebalance", {"proposal_id": plan["proposal_id"]})
            return broker
    assert run(go()).orders == {}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_works_on_both_protocol_versions(mode):
    async def go():
        _, _, server = make()
        async with Client(server, elicitation_callback=user_says(True), mode=mode) as c:
            plan = await call(c, "plan_rebalance")
            return c.protocol_version, await call(c, "execute_rebalance", {"proposal_id": plan["proposal_id"]})
    version, res = run(go())
    assert version >= "2025-11-25" and res["status"] == "executed"


def test_stale_proposal_refused_through_mcp():
    async def go():
        broker, _, server = make()
        async with Client(server, elicitation_callback=user_says(True)) as c:
            plan = await call(c, "plan_rebalance")
            broker.qty["VTI"] += 1                # holdings change before execution
            return broker, await call(c, "execute_rebalance", {"proposal_id": plan["proposal_id"]})
    broker, res = run(go())
    assert res["status"] == "refused" and any("holdings changed" in e for e in res["errors"])
    assert broker.orders == {}


def test_change_targets_with_confirmation():
    yes = user_says(True)

    async def go():
        _, _, server = make()
        async with Client(server, elicitation_callback=yes) as c:
            args = {"targets_percent": {"us_stocks": 50, "bonds": 30, "cash": 20}}
            preview = await call(c, "preview_target_allocation", args)
            res = await call(c, "save_target_allocation", args)
            return preview, res, await call(c, "get_rules")
    preview, res, rules = run(go())
    assert preview["status"] == "needs_confirmation"
    assert res["status"] == "saved" and rules["targets"]["bonds"] == "30.0%"
    assert "Save new target allocation" in yes.asked[0]
    assert rules["constraints"]["max_single_stock"] == "30.0%"   # safety limits untouched


def test_100_percent_nvidia_rejected_before_asking_user():
    yes = user_says(True)

    async def go():
        _, _, server = make()
        async with Client(server, elicitation_callback=yes) as c:
            args = {"targets_percent": {"us_stocks": 100}, "buy_funds": {"us_stocks": "NVDA"}}
            preview = await call(c, "preview_target_allocation", args)
            saved = await c.call_tool("save_target_allocation", args)
            return preview, saved
    preview, saved = run(go())
    assert preview["status"] == "invalid" and "single stock" in preview["errors"][0]
    assert saved.is_error and "single stock" in saved.content[0].text
    assert yes.asked == []                       # never even bothered the user


def test_targets_must_add_to_100():
    async def go():
        _, _, server = make()
        async with Client(server, elicitation_callback=user_says(True)) as c:
            return await call(c, "preview_target_allocation", {"targets_percent": {"us_stocks": 70, "bonds": 40}})
    assert run(go())["status"] == "invalid"
