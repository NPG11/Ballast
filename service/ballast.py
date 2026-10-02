"""The full Ballast loop: READ -> DRIFT -> PLAN -> APPROVE -> EXECUTE -> VERIFY.

This is what the MCP tools will call. Every method returns a plain dict that is easy
to send as JSON and easy for the agent to turn into a short spoken answer.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

from adapters.broker import FAILED, FILLED, Broker
from engine import (AuditLog, Confirmation, Proposal, Rules, RulesError, Side, Status,
                    UnclassifiedHoldingError, approve, check_drift, check_trades, compute_allocation,
                    create_proposal, pct, plan_rebalance, reject, review_rules_change, supersede,
                    validate_for_execution)


class MemoryStore:
    """In-memory storage. Swapped for DynamoDB later with the same methods."""

    def __init__(self):
        self.rules: dict[str, Rules] = {}
        self.proposals: dict[str, Proposal] = {}

    def get_rules(self, user_id: str) -> Rules | None:
        return self.rules.get(user_id)

    def save_rules(self, user_id: str, rules: Rules) -> None:
        self.rules[user_id] = rules

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self.proposals.get(proposal_id)

    def save_proposal(self, proposal: Proposal) -> None:
        self.proposals[proposal.id] = proposal

    def open_proposals(self, user_id: str) -> list[Proposal]:
        """Proposals for this user that are still awaiting approval or approved."""
        return [p for p in self.proposals.values()
                if p.user_id == user_id and p.status in (Status.AWAITING_APPROVAL, Status.APPROVED)]


def _weights(values: dict[str, Decimal], total: Decimal) -> dict[str, str]:
    return {c: pct(v / total) for c, v in sorted(values.items())} if total else {}


def _trade_dict(t) -> dict:
    return {"side": t.side.value, "symbol": t.symbol, "amount": f"{t.notional:.2f}", "category": t.category}


class Ballast:
    def __init__(self, broker: Broker, store: MemoryStore | None = None, audit: AuditLog | None = None,
                 overrides: dict[str, str] | None = None, fill_timeout: float = 30.0,
                 poll_interval: float = 1.0, sleep: Callable[[float], None] = time.sleep):
        self.broker = broker
        self.store = store or MemoryStore()
        self.audit = audit or AuditLog()
        self.overrides = overrides
        self.fill_timeout = fill_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep

    # ---------- rules ----------

    def get_rules(self, user_id: str) -> dict:
        rules = self.store.get_rules(user_id)
        if rules is None:
            return {"status": "no_rules", "message": "No rules saved yet."}
        c = rules.constraints
        return {
            "status": "ok",
            "targets": {k: pct(v) for k, v in sorted(rules.targets.items())},
            "drift_limit": pct(rules.drift_limit),
            "constraints": {
                "max_order": pct(c.max_trade_pct),
                "max_rebalance": f"{c.max_rebalance_usd:.2f}" if c.max_rebalance_usd else None,
                "max_category": pct(c.max_category_pct) if c.max_category_pct else None,
                "max_single_stock": pct(c.max_single_stock_pct) if c.max_single_stock_pct else None,
            },
        }

    def set_rules(self, user_id: str, rules: Rules, confirmed: bool = False,
                  channel: Confirmation = Confirmation.VOICE) -> dict:
        """Call with confirmed=False to preview, then confirmed=True after the user agrees.
        Changes to safety limits (constraints) can only be confirmed on the web app."""
        review = review_rules_change(self.store.get_rules(user_id), rules)
        if not review.ok:
            self.audit.record(user_id, "rules_rejected", errors=review.errors)
            return {"status": "invalid", "errors": review.errors}
        if not confirmed:
            return {"status": "needs_confirmation", "changes": review.changes,
                    "confirm_on": review.required.value}
        if review.required == Confirmation.WEB and channel != Confirmation.WEB:
            return {"status": "needs_web_confirmation", "changes": review.changes,
                    "message": "Changes to your safety limits must be confirmed on the Ballast web app."}
        self.store.save_rules(user_id, rules)
        self.audit.record(user_id, "rules_saved", changes=review.changes, version=rules.version)
        return {"status": "saved", "changes": review.changes}

    # ---------- read + drift ----------

    def portfolio_status(self, user_id: str) -> dict:
        rules = self.store.get_rules(user_id)
        portfolio = self.broker.get_portfolio()
        try:
            alloc = compute_allocation(portfolio, self.overrides)
        except UnclassifiedHoldingError as e:
            return {"status": "unclassified_holdings", "symbols": e.symbols,
                    "message": "I need to know which category these belong to before I can help."}
        result = {
            "status": "ok",
            "total_value": f"{alloc.total_value:.2f}",
            "allocation": _weights(alloc.values, alloc.total_value),
            "market_open": self.broker.is_market_open(),
        }
        if rules is None:
            result["status"] = "no_rules"
            return result
        report = check_drift(alloc, rules)
        result["needs_rebalance"] = report.needs_rebalance
        result["drift_limit"] = pct(rules.drift_limit)
        result["drift"] = [
            {"category": r.category, "target": pct(r.target), "current": pct(r.current),
             "drift_points": f"{r.drift * 100:+.1f}", "over_limit": r.over_limit}
            for r in report.categories
        ]
        return result

    # ---------- plan ----------

    def plan_rebalance(self, user_id: str) -> dict:
        rules = self.store.get_rules(user_id)
        if rules is None:
            return {"status": "no_rules", "message": "Set your target allocation first."}
        portfolio = self.broker.get_portfolio()
        try:
            alloc = compute_allocation(portfolio, self.overrides)
            report = check_drift(alloc, rules)
            if not report.needs_rebalance:
                return {"status": "on_track", "message": "You're within your drift limit. No trades needed."}
            plan = plan_rebalance(portfolio, rules, self.overrides)
        except (UnclassifiedHoldingError, RulesError) as e:
            return {"status": "error", "message": str(e)}

        if not plan.trades:
            return {"status": "on_track", "message": "Drift is over the limit, but every fix is below your minimum trade size."}

        market_open = self.broker.is_market_open()
        checks = check_trades(portfolio, plan.trades, rules, market_open=market_open,
                              buying_power=self.broker.buying_power(), overrides=self.overrides)
        if not checks.ok:
            self.audit.record(user_id, "plan_blocked", violations=checks.violations)
            return {"status": "blocked", "reasons": checks.violations,
                    "trades": [_trade_dict(t) for t in plan.trades]}

        proposal = create_proposal(user_id, plan.trades, portfolio, rules)
        # Only one open plan at a time: close any older one and say why in the log.
        for old in self.store.open_proposals(user_id):
            if supersede(old):
                self.store.save_proposal(old)
                self.audit.record(user_id, "proposal_superseded", old.id, replaced_by=proposal.id)
        self.store.save_proposal(proposal)
        self.audit.record(user_id, "proposal_created", proposal.id,
                          trades=[_trade_dict(t) for t in plan.trades])
        return {
            "status": "awaiting_approval",
            "proposal_id": proposal.id,
            "expires_at": proposal.expires_at.isoformat(),
            "trades": [_trade_dict(t) for t in plan.trades],
            "after": _weights(plan.after_values, alloc.total_value),
            "warnings": checks.warnings,
            "message": "No trade has been placed. Ask the user to approve.",
        }

    # ---------- approve / reject ----------

    def approve(self, user_id: str, proposal_id: str) -> dict:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return {"status": "not_found"}
        errors = approve(p, user_id)
        self.store.save_proposal(p)
        if errors:
            self.audit.record(user_id, "approval_failed", p.id, errors=errors)
            return {"status": "error", "errors": errors}
        self.audit.record(user_id, "proposal_approved", p.id)
        return {"status": "approved", "proposal_id": p.id}

    def reject(self, user_id: str, proposal_id: str) -> dict:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return {"status": "not_found"}
        errors = reject(p, user_id)
        self.store.save_proposal(p)
        if not errors:
            self.audit.record(user_id, "proposal_rejected", p.id)
        return {"status": "error", "errors": errors} if errors else {"status": "rejected"}

    # ---------- execute + verify ----------

    def execute(self, user_id: str, proposal_id: str) -> dict:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return {"status": "not_found"}
        rules = self.store.get_rules(user_id)
        current = self.broker.get_portfolio()

        errors = validate_for_execution(p, user_id, current, rules) if rules else ["No rules saved."]
        if not errors and not self.broker.is_market_open():
            # Proposal stays approved; nothing is placed while the market is closed.
            return {"status": "market_closed",
                    "message": "The market is closed, so I haven't placed anything. Try again after the open."}
        if not errors:
            checks = check_trades(current, p.trades, rules, buying_power=self.broker.buying_power(),
                                  overrides=self.overrides)
            errors = checks.violations
        if errors:
            if p.status == Status.APPROVED:
                p.status = Status.REFUSED
            self.store.save_proposal(p)
            self.audit.record(user_id, "execution_refused", p.id, errors=errors)
            return {"status": "refused", "errors": errors,
                    "message": "Nothing was traded. I can make a fresh proposal."}

        p.status = Status.EXECUTING
        self.store.save_proposal(p)
        sells = [t for t in p.trades if t.side == Side.SELL]
        buys = [t for t in p.trades if t.side == Side.BUY]

        # Sells first, and wait for them so the cash is really there.
        sell_ids = self._submit(p, sells, offset=0)
        sell_orders = self._wait_for(sell_ids)
        if any(o.status != FILLED for o in sell_orders):
            self.audit.record(user_id, "execution_incomplete", p.id,
                              orders=[self._order_dict(o) for o in sell_orders])
            return {"status": "incomplete", "stage": "sells",
                    "orders": [self._order_dict(o) for o in sell_orders],
                    "message": "Some sells didn't fill, so I didn't place the buys."}

        needed = sum((t.notional for t in buys), Decimal("0"))
        available = self.broker.buying_power()
        if needed > available:
            self.audit.record(user_id, "execution_incomplete", p.id, reason="buying power")
            return {"status": "incomplete", "stage": "buys",
                    "message": f"Buys need ${needed:,.2f} but only ${available:,.2f} is available."}

        buy_ids = self._submit(p, buys, offset=len(sells))
        buy_orders = self._wait_for(buy_ids)
        all_orders = sell_orders + buy_orders
        ok = all(o.status == FILLED for o in all_orders)

        p.status = Status.EXECUTED if ok else Status.EXECUTING
        self.store.save_proposal(p)
        self.audit.record(user_id, "orders_filled" if ok else "execution_incomplete", p.id,
                          orders=[self._order_dict(o) for o in all_orders])

        # Verify by reading the portfolio back from the broker.
        after = self.portfolio_status(user_id)
        return {
            "status": "executed" if ok else "incomplete",
            "orders": [self._order_dict(o) for o in all_orders],
            "allocation_now": after.get("allocation"),
            "still_needs_rebalance": after.get("needs_rebalance"),
        }

    def order_status(self, user_id: str, proposal_id: str) -> dict:
        p = self.store.get_proposal(proposal_id)
        if p is None or p.user_id != user_id:
            return {"status": "not_found"}
        return {"status": p.status.value,
                "orders": [self._order_dict(self.broker.get_order(i)) for i in p.order_ids]}

    def history(self, user_id: str) -> list[dict]:
        return [{"at": e.at.isoformat(), "event": e.event, "proposal_id": e.proposal_id, **e.details}
                for e in self.audit.events(user_id)]

    # ---------- helpers ----------

    def _submit(self, p: Proposal, trades, offset: int) -> list[str]:
        ids = []
        for i, t in enumerate(trades, start=offset):
            oid = self.broker.submit_order(t, client_order_id=f"{p.id}-{i}")
            p.order_ids.append(oid)
            ids.append(oid)
        self.store.save_proposal(p)
        return ids

    def _wait_for(self, order_ids: list[str]):
        deadline = time.monotonic() + self.fill_timeout
        while True:
            orders = [self.broker.get_order(i) for i in order_ids]
            done = all(o.status in (FILLED, FAILED) for o in orders)
            if done or time.monotonic() >= deadline:
                return orders
            self.sleep(self.poll_interval)

    @staticmethod
    def _order_dict(o) -> dict:
        return {"id": o.id, "symbol": o.symbol, "side": o.side, "status": o.status,
                "filled": f"{o.filled_notional:.2f}"}
