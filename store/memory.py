"""In-memory storage: used for tests and local runs. DynamoStore has the same methods."""
from __future__ import annotations

import copy
import threading

from engine import AuditLog, Proposal, Rules, Status

OPEN = (Status.AWAITING_APPROVAL, Status.APPROVED)
PENDING = "pending"


class MemoryStore:
    def __init__(self):
        self.rules: dict[str, Rules] = {}
        self.proposals: dict[str, Proposal] = {}
        self.state: dict[str, dict] = {}
        self.confirms: dict[str, dict] = {}
        self.counters: dict[str, int] = {}
        self.audit = AuditLog()
        self._lock = threading.Lock()

    # ---- rules ----
    def get_rules(self, user_id: str) -> Rules | None:
        return self.rules.get(user_id)

    def save_rules(self, user_id: str, rules: Rules) -> None:
        self.rules[user_id] = rules

    # ---- proposals ----
    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self.proposals.get(proposal_id)

    def save_proposal(self, proposal: Proposal) -> None:
        self.proposals[proposal.id] = proposal

    def open_proposals(self, user_id: str) -> list[Proposal]:
        """Proposals for this user that are still awaiting approval or approved."""
        return [p for p in self.proposals.values() if p.user_id == user_id and p.status in OPEN]

    # ---- small JSON documents: demo portfolio, conversation history ----
    def get_state(self, key: str) -> dict | None:
        v = self.state.get(key)
        return copy.deepcopy(v) if v is not None else None

    def put_state(self, key: str, value: dict) -> None:
        self.state[key] = copy.deepcopy(value)

    # ---- confirm cards: one request waits, another answers ----
    def create_confirm(self, cid: str, owner: str, message: str) -> None:
        self.confirms[cid] = {"owner": owner, "message": message, "answer": PENDING}

    def get_confirm(self, cid: str) -> dict | None:
        c = self.confirms.get(cid)
        return dict(c) if c else None

    def answer_confirm(self, cid: str, owner: str, approve: bool) -> bool:
        """Answer an open card. Only its owner can, and only once. Returns True if this call answered it."""
        with self._lock:
            c = self.confirms.get(cid)
            if not c or c["owner"] != owner or c["answer"] != PENDING:
                return False
            c["answer"] = "yes" if approve else "no"
            return True

    # ---- counters (daily usage cap) ----
    def incr(self, key: str) -> int:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + 1
            return self.counters[key]
