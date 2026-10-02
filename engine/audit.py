"""Append-only record of everything Ballast does. Shown in the demo, stored in DynamoDB later."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    at: datetime
    user_id: str
    event: str                    # e.g. proposal_created, execution_refused, orders_filled
    proposal_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class AuditLog:
    def __init__(self):
        self._events: list[AuditEvent] = []

    def record(self, user_id: str, event: str, proposal_id: str | None = None, **details) -> AuditEvent:
        e = AuditEvent(datetime.now(timezone.utc), user_id, event, proposal_id, details)
        self._events.append(e)
        return e

    def events(self, user_id: str | None = None, proposal_id: str | None = None) -> list[AuditEvent]:
        return [e for e in self._events
                if (user_id is None or e.user_id == user_id)
                and (proposal_id is None or e.proposal_id == proposal_id)]
