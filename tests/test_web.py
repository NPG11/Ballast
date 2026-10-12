"""Demo site tests: offline model + demo portfolios, through the real HTTP endpoints."""
import asyncio
import json
import os

os.environ["BALLAST_LLM"] = "offline"
os.environ["BALLAST_BROKER"] = "fake"
os.environ.pop("BALLAST_TABLE", None)
os.environ.pop("BALLAST_MCP_TOKEN", None)

from starlette.testclient import TestClient  # noqa: E402

from agent.offline import OfflineLLM  # noqa: E402
from store.memory import MemoryStore  # noqa: E402
from web.server import COOKIE, Config, create_app, run_turn, trim_history  # noqa: E402


def make(**kw):
    cfg = Config(store=MemoryStore(), llm=OfflineLLM(), confirm_poll=0.01, **kw)
    return cfg, create_app(cfg)


def events(resp):
    return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]


def say(client, text):
    with client.stream("POST", "/api/chat", json={"text": text}) as r:
        return events(r)


def test_page_portfolio_and_session_cookie():
    _, app = make()
    with TestClient(app) as c:
        assert "Ballast" in c.get("/").text
        r = c.get("/api/portfolio")
        assert COOKIE in r.cookies
        p = r.json()
        assert p["total_value"] == "12400.00" and p["needs_rebalance"] is True
        assert p["targets"]["us_stocks"] == "60.0%"


def test_question_streams_tool_call_then_reply():
    _, app = make()
    with TestClient(app) as c:
        evs = say(c, "How's my portfolio?")
        assert [e["type"] for e in evs] == ["tool", "reply"]
        assert evs[0]["tool"] == "get_portfolio" and "12,400" in evs[1]["text"]


def test_each_visitor_has_their_own_demo_portfolio():
    cfg, app = make()
    with TestClient(app) as a, TestClient(app) as b:
        a.get("/api/portfolio"), b.get("/api/portfolio")
        assert a.cookies[COOKIE] != b.cookies[COOKIE]
        user_a = cfg.user_for(a.cookies[COOKIE])
        app_a = cfg.ballast_for(user_a)
        pid = app_a.plan_rebalance(user_a)["proposal_id"]
        app_a.approve(user_a, pid)
        assert app_a.execute(user_a, pid)["status"] == "executed"
        assert a.get("/api/portfolio").json()["needs_rebalance"] is False
        assert b.get("/api/portfolio").json()["needs_rebalance"] is True     # B untouched


def test_rebalance_waits_for_the_card_and_history_survives_between_requests():
    """'Rebalance me' and 'yes' are separate requests that could hit different machines:
    the second only works because the conversation was saved to the store."""
    cfg, _ = make()

    async def go():
        q1 = asyncio.Queue()
        await run_turn(cfg, "sessionAAAAAAAAAAAA", "Rebalance me", q1, {"cids": set()})
        cfg.locks.clear()                                  # as if the next request lands elsewhere
        q2, turn = asyncio.Queue(), {"cids": set()}
        task = asyncio.create_task(run_turn(cfg, "sessionAAAAAAAAAAAA", "yes", q2, turn))
        card = await asyncio.wait_for(q2.get(), 5)
        assert card["type"] == "confirm" and not task.done()
        assert cfg.store.answer_confirm(card["id"], "someone-else", True) is False
        assert cfg.store.answer_confirm(card["id"], "sessionAAAAAAAAAAAA", True) is True
        await task
        rest = []
        while not q2.empty():
            rest.append(q2.get_nowait())
        return card, rest

    card, rest = asyncio.run(go())
    assert card["trades"] == [{"side": "sell", "amount": "960.00", "symbol": "VTI"},
                              {"side": "buy", "amount": "480.00", "symbol": "BND"}]
    types = [e["type"] for e in rest if e]
    assert types == ["confirmed", "tool", "reply"] and rest[0]["approve"] is True
    assert rest[1]["output"]["status"] == "executed"
    status = cfg.ballast_for("web-sessionAAAAAAAAAAAA").portfolio_status("web-sessionAAAAAAAAAAAA")
    assert status["needs_rebalance"] is False


def test_unanswered_card_times_out_as_no():
    cfg, _ = make(confirm_timeout=0.1)

    async def go():
        await run_turn(cfg, "sessionBBBBBBBBBBBB", "Rebalance me", asyncio.Queue(), {"cids": set()})
        q = asyncio.Queue()
        await run_turn(cfg, "sessionBBBBBBBBBBBB", "yes", q, {"cids": set()})
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    out = asyncio.run(go())
    assert [e["type"] for e in out if e][:2] == ["confirm", "confirmed"] and out[1]["approve"] is False
    assert cfg.ballast_for("web-sessionBBBBBBBBBBBB").portfolio_status("web-sessionBBBBBBBBBBBB")["needs_rebalance"]


def test_reset_gives_a_fresh_session():
    _, app = make()
    with TestClient(app) as c:
        c.get("/api/portfolio")
        before = c.cookies[COOKIE]
        c.post("/api/reset")
        assert c.cookies[COOKIE] != before


def test_daily_limit():
    _, app = make(daily_turn_limit=1)
    with TestClient(app) as c:
        assert [e["type"] for e in say(c, "How's my portfolio?")][-1] == "reply"
        r = c.post("/api/chat", json={"text": "again"})
        assert r.status_code == 429 and "limit" in r.json()["error"]


def test_empty_message_is_rejected():
    _, app = make()
    with TestClient(app) as c:
        assert c.post("/api/chat", json={"text": "  "}).status_code == 400


def test_trim_history_starts_at_a_user_message():
    msgs = []
    for i in range(30):
        msgs += [{"role": "user", "content": [{"text": f"q{i}"}]},
                 {"role": "assistant", "content": [{"toolUse": {"toolUseId": f"t{i}", "name": "x", "input": {}}}]},
                 {"role": "user", "content": [{"toolResult": {"toolUseId": f"t{i}", "content": [], "status": "success"}}]},
                 {"role": "assistant", "content": [{"text": f"a{i}"}]}]
    kept = trim_history(msgs, 10)
    assert len(kept) <= 10 and kept[0]["content"][0].get("text", "").startswith("q")


def test_mcp_endpoint_over_real_http_with_token_and_user_confirmation():
    """A real MCP client over real HTTP: token required, and the confirmation round trip spans
    separate requests (the way it must work on Lambda)."""
    import socket
    import threading
    import time

    import httpx2
    import mcp.types as t
    import uvicorn
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    _, app = make(mcp_token="s3cret-token")
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/mcp"
    asked = []

    async def yes(ctx, params):
        asked.append(params.message)
        return t.ElicitResult(action="accept", content={"confirm": True})

    async def go():
        async with httpx2.AsyncClient() as h:
            denied = (await h.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code
        hc = httpx2.AsyncClient(headers={"Authorization": "Bearer s3cret-token"})
        async with Client(streamable_http_client(url, http_client=hc), elicitation_callback=yes) as c:
            names = {x.name for x in (await c.list_tools()).tools}
            plan = json.loads((await c.call_tool("plan_rebalance", {})).content[0].text)
            done = json.loads((await c.call_tool("execute_rebalance",
                                                 {"proposal_id": plan["proposal_id"]})).content[0].text)
        return denied, names, plan, done

    try:
        denied, names, plan, done = asyncio.run(asyncio.wait_for(go(), 30))
    finally:
        server.should_exit = True
    assert denied == 401
    assert {"get_portfolio", "plan_rebalance", "execute_rebalance"} <= names
    assert asked and plan["proposal_id"] in asked[0]
    assert done["status"] == "executed"
