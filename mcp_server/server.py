"""Ballast MCP server.

Exposes the Ballast service as MCP tools over Streamable HTTP.

Key safety rule: the AI can never approve anything itself. Saving rules and executing
a rebalance both ask the *user* directly through MCP elicitation (a confirmation the
client shows to the person) before the tool body runs. If the user declines, or the
client can't ask the user at all, nothing happens.

Run locally:
    python -m mcp_server.server            # http://127.0.0.1:8000/mcp

Env vars:
    BALLAST_BROKER   fake (default) | alpaca
    BALLAST_USER     user id for this single-user demo (default: demo)
    BALLAST_HOST / BALLAST_PORT
"""

import asyncio
import os
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import ToolAnnotations

from engine import Constraints, Rules
from engine.categories import DEFAULT_BUY_SYMBOL
from service.ballast import Ballast

INSTRUCTIONS = """\
Ballast keeps a PAPER-TRADING portfolio aligned with allocation rules the user set.
- Ballast never recommends investments. It only enforces the user's own targets and limits.
- Always call get_portfolio before talking about drift. Never calculate numbers yourself.
- Trades only happen through plan_rebalance followed by execute_rebalance. The user is asked
  to confirm directly; you cannot approve on their behalf.
- To change targets: call preview_target_allocation, tell the user what would change, then
  call save_target_allocation. The user confirms the save directly.
- Keep spoken answers short: the key number first, then one sentence of explanation.
- Safety limits (max order size, single-stock cap, etc.) cannot be changed here. They can
  only be changed on the Ballast web app.
"""

DEMO_RULES = Rules(
    targets={"us_stocks": Decimal("0.60"), "bonds": Decimal("0.20"), "cash": Decimal("0.20")},
    constraints=Constraints(
        max_trade_pct=Decimal("0.25"),
        max_rebalance_usd=Decimal("25000"),
        max_category_pct=Decimal("0.80"),
        max_single_stock_pct=Decimal("0.30"),
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


class Confirm(BaseModel):
    confirm: bool = Field(description="Yes, go ahead")



def _make_broker():
    kind = os.environ.get("BALLAST_BROKER", "fake").lower()
    if kind == "alpaca":
        from adapters.alpaca import AlpacaBroker
        return AlpacaBroker()
    from adapters.fake import FakeBroker
    # A drifted demo portfolio: stocks ran up, so US stocks are ~68% vs a 60% target.
    return FakeBroker({"VTI": ("30", "280"), "BND": ("25", "80")}, cash="2000")


def build_server(app: Ballast | None = None, user_id: str | None = None) -> MCPServer:
    app = app or Ballast(_make_broker())
    user = user_id or os.environ.get("BALLAST_USER", "demo")
    if app.store.get_rules(user) is None:
        app.store.save_rules(user, DEMO_RULES)

    mcp = MCPServer(name="ballast", title="Ballast", version="0.1.0", instructions=INSTRUCTIONS)

    async def run(fn, *args):
        # Service calls may hit the network (Alpaca) or wait for fills: keep the event loop free.
        return await asyncio.to_thread(fn, *args)

    def accepted(decision) -> bool:
        return isinstance(decision, AcceptedElicitation) and decision.data.confirm

    def build_rules(targets_percent, drift_limit_percent, buy_funds) -> Rules:
        """Turn tool arguments into Rules, keeping the user's current safety limits."""
        current = app.store.get_rules(user)
        return Rules(
            targets={k: Decimal(str(v)) / 100 for k, v in targets_percent.items()},
            drift_limit=(Decimal(str(drift_limit_percent)) / 100 if drift_limit_percent is not None
                         else current.drift_limit if current else Decimal("0.05")),
            min_trade_usd=current.min_trade_usd if current else Decimal("50"),
            buy_symbols=buy_funds if buy_funds is not None else (current.buy_symbols if current else {}),
            constraints=current.constraints if current else DEMO_RULES.constraints,
        )

    # Resolvers: run BEFORE the tool body and ask the user. Must be deterministic.

    def confirm_execute(proposal_id: str) -> Elicit[Confirm]:
        p = app.store.get_proposal(proposal_id)
        if p is None or p.user_id != user:
            raise ToolError(f"No proposal called {proposal_id}.")
        lines = [f"{t.side.value.upper()} ${t.notional:,.2f} {t.symbol}" for t in p.trades]
        return Elicit(f"Confirm paper rebalance {proposal_id}:\n" + "\n".join(lines)
                      + "\nPaper trading only.", Confirm)

    def confirm_rules(targets_percent: dict[str, float], drift_limit_percent: float | None = None,
                      buy_funds: dict[str, str] | None = None) -> Elicit[Confirm]:
        try:
            new = build_rules(targets_percent, drift_limit_percent, buy_funds)
        except Exception as e:
            raise ToolError(str(e))
        preview = app.set_rules(user, new)
        if preview["status"] != "needs_confirmation":
            raise ToolError("; ".join(preview.get("errors") or [preview.get("message", "Can't save these rules.")]))
        return Elicit("Save new target allocation?\n" + "\n".join(preview["changes"]), Confirm)

    # ---------------- read tools ----------------

    @mcp.tool(annotations=READ_ONLY)
    async def get_portfolio() -> dict:
        """Get the user's paper portfolio: total value, current split by category, and how far
        each category has drifted from the user's targets. Use this for questions like
        "how's my portfolio?" or "am I on track?"."""
        return await run(app.portfolio_status, user)

    @mcp.tool(annotations=READ_ONLY)
    async def get_rules() -> dict:
        """Get the user's saved target allocation, drift limit, and safety limits."""
        return await run(app.get_rules, user)

    @mcp.tool(annotations=READ_ONLY)
    async def list_categories() -> dict:
        """List the categories Ballast understands and the default fund used to buy each one.
        Use this to map phrases like "international" or "real estate" to category names."""
        return {"categories": {**{c: f for c, f in sorted(DEFAULT_BUY_SYMBOL.items())}, "cash": None}}

    @mcp.tool(annotations=READ_ONLY)
    async def get_order_status(proposal_id: str) -> dict:
        """Check the status of the orders placed for a proposal."""
        return await run(app.order_status, user, proposal_id)

    @mcp.tool(annotations=READ_ONLY)
    async def get_activity() -> dict:
        """The audit log: every proposal, approval, refusal and fill, oldest first."""
        return {"events": await run(app.history, user)}

    # ---------------- planning ----------------

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
    async def plan_rebalance() -> dict:
        """Work out the trades that would bring the portfolio back to the user's targets.
        This places NO trades. It returns a proposal the user can then confirm with
        execute_rebalance. Proposals expire after 5 minutes."""
        return await run(app.plan_rebalance, user)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def preview_target_allocation(
        targets_percent: dict[str, float],
        drift_limit_percent: float | None = None,
        buy_funds: dict[str, str] | None = None,
    ) -> dict:
        """Check a new target allocation without saving it, e.g.
        {"us_stocks": 60, "bonds": 30, "cash": 10}. Percentages must add up to 100. Category
        names come from list_categories. Optional buy_funds picks the fund for a category, e.g.
        {"us_stocks": "VOO"}. Returns what would change, or why the rules aren't allowed."""
        try:
            new = build_rules(targets_percent, drift_limit_percent, buy_funds)
        except Exception as e:
            return {"status": "invalid", "errors": [str(e)]}
        return await run(app.set_rules, user, new)

    # ---------------- writes (always confirmed by the user) ----------------

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                          idempotent_hint=False, open_world_hint=True))
    async def execute_rebalance(
        proposal_id: str,
        decision: Annotated[ElicitationResult[Confirm], Resolve(confirm_execute)],
    ) -> dict:
        """Place the paper orders for a proposal from plan_rebalance. Before anything runs,
        the user is asked to confirm directly. Ballast then re-checks the proposal and refuses
        if it expired or if the holdings or rules changed."""
        if not accepted(decision):
            await run(app.reject, user, proposal_id)
            return {"status": "cancelled", "message": "The user declined. Nothing was traded."}
        approved = await run(app.approve, user, proposal_id)
        if approved["status"] != "approved":
            return approved
        return await run(app.execute, user, proposal_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    async def save_target_allocation(
        targets_percent: dict[str, float],
        decision: Annotated[ElicitationResult[Confirm], Resolve(confirm_rules)],
        drift_limit_percent: float | None = None,
        buy_funds: dict[str, str] | None = None,
    ) -> dict:
        """Save a new target allocation (same arguments as preview_target_allocation).
        Safety limits are kept as they are and cannot be changed with this tool.
        The user is asked to confirm directly before anything is saved."""
        if not accepted(decision):
            return {"status": "cancelled", "message": "Nothing was changed."}
        new = build_rules(targets_percent, drift_limit_percent, buy_funds)
        return await run(app.set_rules, user, new, True)

    return mcp


def main():
    host = os.environ.get("BALLAST_HOST", "127.0.0.1")
    port = int(os.environ.get("BALLAST_PORT", "8000"))
    build_server().run("streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
