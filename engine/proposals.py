"""Step 5: proposals. The AI can ask for a plan, but nothing executes without one of these.

Lifecycle:  AWAITING_APPROVAL -> APPROVED -> EXECUTING -> EXECUTED
                     or -> EXPIRED / REJECTED / REFUSED (revalidation failed)
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from .models import Portfolio, Rules, Trade

DEFAULT_TTL = timedelta(minutes=5)


class Status(str, Enum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REFUSED = "refused"


def portfolio_fingerprint(portfolio: Portfolio) -> str:
    """Identifies holdings by quantity, not price (prices move every second).
    If holdings change between proposal and execution, the proposal is stale."""
    parts = sorted(f"{h.symbol}:{h.qty.normalize()}" for h in portfolio.holdings)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Proposal:
    id: str
    user_id: str
    trades: list[Trade]
    portfolio_version: str
    rules_version: str
    created_at: datetime
    expires_at: datetime
    status: Status = Status.AWAITING_APPROVAL
    order_ids: list[str] = field(default_factory=list)


def create_proposal(user_id: str, trades: list[Trade], portfolio: Portfolio, rules: Rules,
                    now: datetime | None = None, ttl: timedelta = DEFAULT_TTL) -> Proposal:
    now = now or _now()
    return Proposal(
        id="BL-" + secrets.token_hex(2).upper(),
        user_id=user_id,
        trades=list(trades),
        portfolio_version=portfolio_fingerprint(portfolio),
        rules_version=rules.version,
        created_at=now,
        expires_at=now + ttl,
    )


def approve(proposal: Proposal, user_id: str, now: datetime | None = None) -> list[str]:
    """Record the user's explicit approval. Returns errors (empty list = approved)."""
    now = now or _now()
    errors = _basic_checks(proposal, user_id, now)
    if not errors and proposal.status != Status.AWAITING_APPROVAL:
        errors.append(f"This proposal is already {proposal.status.value.replace('_', ' ')}.")
    if not errors:
        proposal.status = Status.APPROVED
    return errors


def reject(proposal: Proposal, user_id: str) -> list[str]:
    if proposal.user_id != user_id:
        return ["This proposal belongs to a different user."]
    if proposal.status in (Status.AWAITING_APPROVAL, Status.APPROVED):
        proposal.status = Status.REJECTED
        return []
    return [f"This proposal is already {proposal.status.value.replace('_', ' ')}."]


def validate_for_execution(proposal: Proposal, user_id: str, current_portfolio: Portfolio,
                           current_rules: Rules, now: datetime | None = None) -> list[str]:
    """Final checks right before placing orders. Returns errors (empty list = safe to execute)."""
    now = now or _now()
    errors = _basic_checks(proposal, user_id, now)
    if proposal.status != Status.APPROVED:
        errors.append("This proposal hasn't been approved.")
    if portfolio_fingerprint(current_portfolio) != proposal.portfolio_version:
        errors.append("Your holdings changed after this proposal was created, so I won't execute it.")
    if current_rules.version != proposal.rules_version:
        errors.append("Your rules changed after this proposal was created, so I won't execute it.")
    return errors


def _basic_checks(proposal: Proposal, user_id: str, now: datetime) -> list[str]:
    errors = []
    if proposal.user_id != user_id:
        errors.append("This proposal belongs to a different user.")
    if now > proposal.expires_at:
        if proposal.status in (Status.AWAITING_APPROVAL, Status.APPROVED):
            proposal.status = Status.EXPIRED
        errors.append("This proposal expired. I'll make a fresh one with current prices.")
    return errors
