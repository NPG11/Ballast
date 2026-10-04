"""Ballast demo site: an Alexa+-style assistant in the browser.

    python -m web.server                      # http://127.0.0.1:8080, Claude on Bedrock, fake broker
    BALLAST_LLM=offline python -m web.server  # no AWS needed (keyword rules instead of Claude)
    BALLAST_BROKER=alpaca python -m web.server

How a turn works:
    POST /api/chat {"text": ...}  -> a stream of events (server-sent events):
        {"type": "tool", ...}       a tool call and its result (for the "under the hood" panel)
        {"type": "confirm", ...}    the MCP server is asking the USER to confirm; the page shows a card
        {"type": "reply", ...}      what Ballast says
    POST /api/confirm {"id": ..., "approve": true|false}   the user's answer to a confirm card
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agent.agent import BallastAgent
from mcp_server.server import DEMO_RULES, build_server, make_broker
from service.ballast import Ballast

STATIC = Path(__file__).parent / "static"
USER = os.environ.get("BALLAST_USER", "demo")
CONFIRM_TIMEOUT = 120  # seconds before an unanswered confirm card counts as "no"


def make_llm():
    if os.environ.get("BALLAST_LLM", "bedrock").lower() == "offline":
        from agent.offline import OfflineLLM
        return OfflineLLM()
    from agent.llm import BedrockLLM
    return BedrockLLM()


class Session:
    """One demo session: the Ballast service, its MCP server, and the agent talking to it."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.queue: asyncio.Queue | None = None
        self.workers: set[asyncio.Task] = set()
        self.llm = make_llm()
        self._stack = None

    async def start(self):
        self.app = Ballast(make_broker())
        self.app.store.save_rules(USER, DEMO_RULES)
        self.agent = BallastAgent(build_server(self.app, user_id=USER), self.llm,
                                  confirm=self._confirm, on_event=self._on_event)
        await self.agent.__aenter__()

    async def stop(self):
        await self.agent.__aexit__(None, None, None)

    async def reset(self):
        await self.stop()
        await self.start()

    async def _on_event(self, event: dict):
        if self.queue and event["event"] == "tool_call":
            await self.queue.put({"type": "tool", **event})

    async def _confirm(self, message: str) -> bool:
        cid = secrets.token_hex(4)
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        await self.queue.put({"type": "confirm", "id": cid, "message": message,
                              "trades": _parse_trades(message)})
        try:
            approved = await asyncio.wait_for(fut, CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            approved = False
        finally:
            self.pending.pop(cid, None)
        await self.queue.put({"type": "confirmed", "id": cid, "approve": approved})
        return approved


def _parse_trades(message: str) -> list[dict]:
    """Pull 'SELL $960.00 VTI' lines out of the confirmation text for the confirm card."""
    return [{"side": side.lower(), "amount": amount, "symbol": symbol}
            for side, amount, symbol in re.findall(r"^(SELL|BUY) \$([\d,.]+) (\S+)$", message, re.M)]


session = Session()


@asynccontextmanager
async def lifespan(app):
    await session.start()
    yield
    await session.stop()


async def index(request: Request):
    return FileResponse(STATIC / "index.html")


async def _run_turn(text: str, q: asyncio.Queue):
    """Runs one turn to completion, even if the browser goes away halfway."""
    async with session.lock:
        session.queue = q
        try:
            reply = await session.agent.ask(text)
            await q.put({"type": "reply", "text": reply.text, "error": reply.error})
        except Exception as e:  # never leave the page hanging
            await q.put({"type": "reply", "text": f"Something went wrong: {e}", "error": True})
        finally:
            session.queue = None
            await q.put(None)


async def chat(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Say or type something first."}, status_code=400)
    if session.lock.locked():
        return JSONResponse({"error": "Ballast is still answering the last request."}, status_code=409)

    q: asyncio.Queue = asyncio.Queue()
    worker = asyncio.create_task(_run_turn(text, q))
    session.workers.add(worker)
    worker.add_done_callback(session.workers.discard)

    async def events():
        try:
            while (ev := await q.get()) is not None:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            # Browser left (refresh, closed tab) before the turn ended: an open confirm card
            # counts as "no", so nothing trades for someone who isn't looking.
            if not worker.done():
                for fut in list(session.pending.values()):
                    if not fut.done():
                        fut.set_result(False)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def confirm(request: Request):
    body = await request.json()
    fut = session.pending.get(body.get("id", ""))
    if fut is None or fut.done():
        return JSONResponse({"error": "That confirmation is no longer open."}, status_code=404)
    fut.set_result(bool(body.get("approve")))
    return JSONResponse({"ok": True})


async def portfolio(request: Request):
    status = session.app.portfolio_status(USER)
    status["targets"] = session.app.get_rules(USER).get("targets", {})
    status["broker"] = os.environ.get("BALLAST_BROKER", "fake")
    status["model"] = getattr(session.llm, "model_id", "unknown")
    return JSONResponse(status)


async def activity(request: Request):
    return JSONResponse({"events": session.app.history(USER)})


async def reset(request: Request):
    if os.environ.get("BALLAST_BROKER", "fake") != "fake":
        return JSONResponse({"error": "Reset only works with the demo portfolio."}, status_code=400)
    async with session.lock:
        await session.reset()
    return JSONResponse({"ok": True})


class NoCache:
    """Always send the latest page files, so updates show up without fighting the browser cache."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (scope["path"] == "/" or scope["path"].startswith("/static")):
            return await self.app(scope, receive, send)

        async def send_no_cache(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, send_no_cache)


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/confirm", confirm, methods=["POST"]),
        Route("/api/portfolio", portfolio),
        Route("/api/activity", activity),
        Route("/api/reset", reset, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ],
    lifespan=lifespan,
)
app.add_middleware(NoCache)


def main():
    import uvicorn
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("WEB_PORT", "8080"))
    print(f"Ballast demo on http://{host}:{port}  (model: {getattr(session.llm, 'model_id', '?')}, "
          f"broker: {os.environ.get('BALLAST_BROKER', 'fake')})")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
