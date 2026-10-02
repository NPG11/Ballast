"""Reviewing changes to the user's rules.

Targets can be changed with a normal (voice) confirmation.
Constraints are the user's safety limits, so changing them requires a stronger
confirmation on the web app. Otherwise a voice command could remove the very
limits meant to protect the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Rules, RulesError, pct


class Confirmation(str, Enum):
    VOICE = "voice"   # "yes" in conversation is enough
    WEB = "web"       # must be confirmed on the web app


@dataclass
class RulesChangeReview:
    ok: bool
    required: Confirmation
    changes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def review_rules_change(current: Rules | None, proposed: Rules) -> RulesChangeReview:
    try:
        proposed.validate()
    except RulesError as e:
        return RulesChangeReview(False, Confirmation.VOICE, errors=[str(e)])

    changes: list[str] = []
    if current is None:
        changes.append("Create your first set of rules.")
        return RulesChangeReview(True, Confirmation.WEB, changes)

    for cat in sorted(set(current.targets) | set(proposed.targets)):
        old, new = current.targets.get(cat), proposed.targets.get(cat)
        if old != new:
            label = cat.replace("_", " ")
            changes.append(f"{label}: {pct(old) if old is not None else 'none'} -> "
                           f"{pct(new) if new is not None else 'removed'}")
    if current.drift_limit != proposed.drift_limit:
        changes.append(f"drift limit: {pct(current.drift_limit)} -> {pct(proposed.drift_limit)}")
    if current.buy_symbols != proposed.buy_symbols:
        changes.append("funds used for buying changed")

    constraints_changed = current.constraints != proposed.constraints
    if constraints_changed:
        changes.append("safety limits changed")

    required = Confirmation.WEB if constraints_changed else Confirmation.VOICE
    return RulesChangeReview(True, required, changes)
