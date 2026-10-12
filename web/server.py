"""Ballast demo site: an Alexa+-style assistant in the browser.

    python -m web.server                      # http://127.0.0.1:8080, Claude on Bedrock, demo portfolio
    BALLAST_LLM=offline python -m web.server  # no AWS needed (keyword rules instead of Claude)
    BALLAST_BROKER=alpaca python -m web.server

The server keeps nothing important in memory, so it runs the same on a laptop or on AWS Lambda,
where each request can land on a different machine:
    - each browser gets its own session (cookie) and, in demo mode, its own demo portfolio
    - conversation history, demo portfolios, proposals, the audit log and confirm cards live in
      the store: DynamoDB when BALLAST_TABLE is set, otherwise memory

How a turn works:
    POST /api/chat {"text": ...}  -> a stream of events (server-sent events):
        {"type": "tool", ...}       a tool call and its result (for the "under the hood" panel)
        {"type": "confirm", ...}    the MCP server is asking the USER to confirm; the page shows a card
        {"type": "confirmed", ...}  the card was answered (or timed out, which counts as no)
        {"type": "reply", ...}      what Ballast says
    POST /api/confirm {"id": ..., "approve": true|false}   the user's answer to a confirm card

If BALLAST_MCP_TOKEN is set, the Ballast MCP server is also served at /mcp (Streamable HTTP),
for MCP clients that send "Authorization: Bearer <token>".
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from adapters.stored import StoredDemoBroker
from agent.agent import BallastAgent, repair_history
from mcp_server.server import DEMO_RULES, build_server
from service.ballast import Ballast
from store import make_store

STATIC = Path(__file__).parent / "static"
COOKIE = "ballast_sid"
COOKIE_DAYS = 14
HISTORY_MESSAGES = 40      # conversation kept per session (older turns are dropped)
ALPACA_USER = "alpaca-owner"
MCP_DEMO_USER = "mcp-demo"


def make_llm():
    if os.environ.get("BALLAST_LLM", "bedrock").lower() == "offline":
        from agent.offline import OfflineLLM
        return OfflineLLM()
    from agent.llm import BedrockLLM
    return BedrockLLM()


@dataclass
class Config:
    store: object
    llm: object
    demo: bool = True                    # demo portfolio per visitor; False = the Alpaca paper account
    confirm_timeout: float = 120.0       # an unanswered confirm card counts as "no"
    confirm_poll: float = 0.4
    daily_turn_limit: int = 0            # 0 = no limit
    mcp_token: str | None = None
    locks: dict = field(default_factory=dict)
    workers: set = field(default_factory=set)
    _alpaca: object = None

    # ---- who is this, and which portfolio do they see ----
    def user_for(self, sid: str) -> str:
        return f"web-{sid}" if self.demo else ALPACA_USER

    def broker_for(self, user: str):
        if self.demo:
            return StoredDemoBroker(self.store, user)
        if self._alpaca is None:
            from adapters.alpaca import AlpacaBroker
            self._alpaca = AlpacaBroker()
        return self._alpaca

    def ballast_for(self, user: str) -> Ballast:
        app = Ballast(self.broker_for(user), store=self.store)
        if self.store.get_rules(user) is None:
            self.store.save_rules(user, DEMO_RULES)
        return app

    # ---- conversation history, kept in the store between requests ----
    def load_history(self, sid: str) -> list[dict]:
        state = self.store.get_state(f"history:{sid}")
        return state["messages"] if state else []

    def save_history(self, sid: str, messages: list[dict]) -> None:
        self.store.put_state(f"history:{sid}", {"messages": trim_history(messages, HISTORY_MESSAGES)})


def trim_history(messages: list[dict], keep: int) -> list[dict]:
    """Keep about the last `keep` messages, starting at a message the user typed, so the
    conversation stays valid for the model."""
    if len(messages) > keep:
        start = len(messages) - keep
        while start < len(messages) and not (
                messages[start]["role"] == "user" and any("text" in b for b in messages[start]["content"])):
            start += 1
        messages = messages[start:]
    return repair_history(messages)


def _parse_trades(message: str) -> list[dict]:
    """Pull 'SELL $960.00 VTI' lines out of the confirmation text for the confirm card."""
    return [{"side": side.lower(), "amount": amount, "symbol": symbol}
            for side, amount, symbol in re.findall(r"^(SELL|BUY) \$([\d,.]+) (\S+)$", message, re.M)]


# ---------------- sessions ----------------

def _session(request: Request) -> tuple[str, bool]:
    sid = request.cookies.get(COOKIE, "")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", sid):
        return sid, False
    return secrets.token_urlsafe(18), True


def _set_cookie(request: Request, response, sid: str) -> None:
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, sid, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax", secure=https)


def _json(request: Request, data, sid: str, new: bool, status: int = 200):
    resp = JSONResponse(data, status_code=status)
    if new:
        _set_cookie(request, resp, sid)
    return resp


# ---------------- one turn ----------------

async def run_turn(cfg: Config, sid: str, text: str, q: asyncio.Queue, turn: dict) -> None:
    """Runs one turn to completion, even if the browser goes away halfway."""
    lock = cfg.locks.setdefault(sid, asyncio.Lock())
    async with lock:
        try:
            user = cfg.user_for(sid)
            app = await asyncio.to_thread(cfg.ballast_for, user)

            async def on_event(event):
                if event["event"] == "tool_call":
                    await q.put({"type": "tool", **event})

            async def confirm(message: str) -> bool:
                return await ask_user(cfg, sid, message, q, turn)

            agent = BallastAgent(build_server(app, user_id=user), cfg.llm, confirm=confirm, on_event=on_event)
            async with agent:
                agent.history = await asyncio.to_thread(cfg.load_history, sid)
                reply = await agent.ask(text)
                await asyncio.to_thread(cfg.save_history, sid, agent.history)
            await q.put({"type": "reply", "text": reply.text, "error": reply.error})
        except Exception as e:  # never leave the page hanging
            await q.put({"type": "reply", "text": f"Something went wrong: {e}", "error": True})
        finally:
            await q.put(None)


async def ask_user(cfg: Config, sid: str, message: str, q: asyncio.Queue, turn: dict) -> bool:
    """Show a confirm card and wait for the answer. The answer may arrive on another machine,
    so it goes through the store: this request polls until it's answered or times out."""
    cid = secrets.token_hex(6)
    await asyncio.to_thread(cfg.store.create_confirm, cid, sid, message)
    turn["cids"].add(cid)
    await q.put({"type": "confirm", "id": cid, "message": message, "trades": _parse_trades(message)})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.confirm_timeout
    card = None
    while loop.time() < deadline:
        card = await asyncio.to_thread(cfg.store.get_confirm, cid)
        if card and card["answer"] != "pending":
            break
        await asyncio.sleep(cfg.confirm_poll)
    else:
        await asyncio.to_thread(cfg.store.answer_confirm, cid, sid, False)    # timed out: no
        card = await asyncio.to_thread(cfg.store.get_confirm, cid)          # unless they just answered
    turn["cids"].discard(cid)
    approved = bool(card and card["answer"] == "yes")
    await q.put({"type": "confirmed", "id": cid, "approve": approved})
    return approved


# ---------------- routes ----------------

def _cfg(request: Request) -> Config:
    return request.app.state.cfg


async def index(request: Request):
    return FileResponse(STATIC / "index.html")


async def healthz(request: Request):
    return JSONResponse({"ok": True})


async def chat(request: Request):
    cfg = _cfg(request)
    sid, new = _session(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return _json(request, {"error": "Say or type something first."}, sid, new, 400)
    if cfg.locks.get(sid) and cfg.locks[sid].locked():
        return _json(request, {"error": "Ballast is still answering the last request."}, sid, new, 409)
    if cfg.daily_turn_limit:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if await asyncio.to_thread(cfg.store.incr, f"turns:{today}") > cfg.daily_turn_limit:
            return _json(request, {"error": "The demo has reached today's limit. Please try again tomorrow."},
                         sid, new, 429)

    q: asyncio.Queue = asyncio.Queue()
    turn = {"cids": set()}
    worker = asyncio.create_task(run_turn(cfg, sid, text, q, turn))
    cfg.workers.add(worker)
    worker.add_done_callback(cfg.workers.discard)

    async def events():
        try:
            while (ev := await q.get()) is not None:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            # Browser left (refresh, closed tab) mid-turn: an open confirm card counts as "no",
            # so nothing trades for someone who isn't looking.
            if not worker.done():
                for cid in list(turn["cids"]):
                    await asyncio.to_thread(cfg.store.answer_confirm, cid, sid, False)

    resp = StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if new:
        _set_cookie(request, resp, sid)
    return resp


async def confirm(request: Request):
    cfg = _cfg(request)
    sid, new = _session(request)
    body = await request.json()
    ok = await asyncio.to_thread(cfg.store.answer_confirm, str(body.get("id", "")), sid, bool(body.get("approve")))
    if not ok:
        return _json(request, {"error": "That confirmation is no longer open."}, sid, new, 404)
    return _json(request, {"ok": True}, sid, new)


async def portfolio(request: Request):
    cfg = _cfg(request)
    sid, new = _session(request)
    user = cfg.user_for(sid)

    def read():
        app = cfg.ballast_for(user)
        status = app.portfolio_status(user)
        status["targets"] = app.get_rules(user).get("targets", {})
        return status

    status = await asyncio.to_thread(read)
    status["broker"] = "fake" if cfg.demo else "alpaca"
    status["model"] = getattr(cfg.llm, "model_id", "unknown")
    return _json(request, status, sid, new)


async def activity(request: Request):
    cfg = _cfg(request)
    sid, new = _session(request)
    user = cfg.user_for(sid)
    events = await asyncio.to_thread(lambda: cfg.ballast_for(user).history(user))
    return _json(request, {"events": events}, sid, new)


async def reset(request: Request):
    cfg = _cfg(request)
    if not cfg.demo:
        return JSONResponse({"error": "Reset only works with the demo portfolio."}, status_code=400)
    sid = secrets.token_urlsafe(18)       # a fresh session = a fresh demo portfolio and conversation
    resp = JSONResponse({"ok": True})
    _set_cookie(request, resp, sid)
    return resp


# ---------------- wrappers ----------------

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


class WithMcp:
    """Send /mcp to the Ballast MCP server (bearer token required), everything else to the site."""

    def __init__(self, site, mcp_app, token: str):
        self.site, self.mcp_app, self.token = site, mcp_app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if not secrets.compare_digest(auth, f"Bearer {self.token}"):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
            return await self.mcp_app(scope, receive, send)
        return await self.site(scope, receive, send)


def create_app(cfg: Config | None = None):
    if cfg is None:
        cfg = Config(
            store=make_store(),
            llm=make_llm(),
            demo=os.environ.get("BALLAST_BROKER", "fake").lower() != "alpaca",
            daily_turn_limit=int(os.environ.get("BALLAST_DAILY_TURN_LIMIT", "0") or 0),
            mcp_token=os.environ.get("BALLAST_MCP_TOKEN") or None,
        )

    mcp = None
    if cfg.mcp_token:
        mcp_user = MCP_DEMO_USER if cfg.demo else ALPACA_USER
        mcp = build_server(cfg.ballast_for(mcp_user), user_id=mcp_user)
        mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if mcp is None:
            yield
            return
        async with mcp.session_manager.run():
            yield

    site = Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", healthz),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/confirm", confirm, methods=["POST"]),
            Route("/api/portfolio", portfolio),
            Route("/api/activity", activity),
            Route("/api/reset", reset, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        lifespan=lifespan,
    )
    site.state.cfg = cfg
    site.add_middleware(NoCache)
    return WithMcp(site, mcp_app, cfg.mcp_token) if mcp else site


app = create_app()


def main():
    import uvicorn
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("WEB_PORT", "8080"))
    print(f"Ballast demo on http://{host}:{port}  (model: {getattr(make_llm(), 'model_id', '?')}, "
          f"broker: {os.environ.get('BALLAST_BROKER', 'fake')}, "
          f"store: {'DynamoDB ' + os.environ['BALLAST_TABLE'] if os.environ.get('BALLAST_TABLE') else 'memory'})")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
