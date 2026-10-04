"""Demo site tests: offline model + demo portfolio, through the real HTTP endpoints."""
import json
import os

os.environ["BALLAST_LLM"] = "offline"
os.environ["BALLAST_BROKER"] = "fake"

from starlette.testclient import TestClient  # noqa: E402

from web.server import app  # noqa: E402


def events(resp):
    return [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]


def test_page_and_portfolio():
    with TestClient(app) as c:
        assert "Ballast" in c.get("/").text
        p = c.get("/api/portfolio").json()
        assert p["total_value"] == "12400.00" and p["needs_rebalance"] is True
        assert p["targets"]["us_stocks"] == "60.0%"


def test_question_streams_tool_call_then_reply():
    with TestClient(app) as c:
        with c.stream("POST", "/api/chat", json={"text": "How's my portfolio?"}) as r:
            evs = events(r)
        assert [e["type"] for e in evs] == ["tool", "reply"]
        assert evs[0]["tool"] == "get_portfolio" and "12,400" in evs[1]["text"]


def test_rebalance_waits_for_the_user_on_the_confirm_card():
    """Same session the server uses: the turn pauses on the confirm card until the user answers."""
    import asyncio
    from web.server import Session

    async def go():
        s = Session()
        await s.start()
        await s.agent.ask("Rebalance me")
        s.queue = asyncio.Queue()
        turn = asyncio.create_task(s.agent.ask("yes"))

        card = await asyncio.wait_for(s.queue.get(), 5)        # the page shows the card...
        assert card["type"] == "confirm" and not turn.done()   # ...and nothing has traded yet
        assert s.app.broker.orders == {}
        s.pending[card["id"]].set_result(True)                 # user clicks "Place paper orders"
        reply = await asyncio.wait_for(turn, 5)
        status = s.app.portfolio_status("demo")
        await s.stop()
        return card, reply, status

    card, reply, status = asyncio.run(go())
    assert card["trades"] == [{"side": "sell", "amount": "960.00", "symbol": "VTI"},
                              {"side": "buy", "amount": "480.00", "symbol": "BND"}]
    assert "Done" in reply.text and status["needs_rebalance"] is False


def test_empty_message_is_rejected():
    with TestClient(app) as c:
        assert c.post("/api/chat", json={"text": "  "}).status_code == 400
