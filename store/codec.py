"""Turn Ballast's objects into plain JSON and back, for storage.

Money and percentages stay exact: Decimals are stored as strings, never floats.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from engine import Constraints, Proposal, Rules, Side, Status, Trade


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(v)


def _str(v) -> str | None:
    return None if v is None else str(v)


def rules_to_dict(r: Rules) -> dict:
    c = r.constraints
    return {
        "targets": {k: str(v) for k, v in r.targets.items()},
        "drift_limit": str(r.drift_limit),
        "min_trade_usd": str(r.min_trade_usd),
        "buy_symbols": dict(r.buy_symbols),
        "constraints": {
            "max_trade_pct": str(c.max_trade_pct),
            "max_rebalance_usd": _str(c.max_rebalance_usd),
            "max_category_pct": _str(c.max_category_pct),
            "max_single_stock_pct": _str(c.max_single_stock_pct),
        },
    }


def rules_from_dict(d: dict) -> Rules:
    c = d.get("constraints", {})
    return Rules(
        targets={k: Decimal(v) for k, v in d["targets"].items()},
        drift_limit=Decimal(d["drift_limit"]),
        min_trade_usd=Decimal(d["min_trade_usd"]),
        buy_symbols=dict(d.get("buy_symbols", {})),
        constraints=Constraints(
            max_trade_pct=Decimal(c.get("max_trade_pct", "0.25")),
            max_rebalance_usd=_dec(c.get("max_rebalance_usd")),
            max_category_pct=_dec(c.get("max_category_pct")),
            max_single_stock_pct=_dec(c.get("max_single_stock_pct")),
        ),
    )


def trade_to_dict(t: Trade) -> dict:
    return {"symbol": t.symbol, "side": t.side.value, "notional": str(t.notional), "category": t.category}


def trade_from_dict(d: dict) -> Trade:
    return Trade(d["symbol"], Side(d["side"]), Decimal(d["notional"]), d["category"])


def proposal_to_dict(p: Proposal) -> dict:
    return {
        "id": p.id, "user_id": p.user_id,
        "trades": [trade_to_dict(t) for t in p.trades],
        "portfolio_version": p.portfolio_version, "rules_version": p.rules_version,
        "created_at": p.created_at.isoformat(), "expires_at": p.expires_at.isoformat(),
        "status": p.status.value, "order_ids": list(p.order_ids),
    }


def proposal_from_dict(d: dict) -> Proposal:
    return Proposal(
        id=d["id"], user_id=d["user_id"],
        trades=[trade_from_dict(t) for t in d["trades"]],
        portfolio_version=d["portfolio_version"], rules_version=d["rules_version"],
        created_at=datetime.fromisoformat(d["created_at"]),
        expires_at=datetime.fromisoformat(d["expires_at"]),
        status=Status(d["status"]), order_ids=list(d.get("order_ids", [])),
    )
