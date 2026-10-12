"""DynamoDB storage. One table, keyed by pk (partition) and sk (sort).

    USER#<u>      RULES                 the user's rules
    PROPOSAL#<id> PROPOSAL              a proposal
    USER#<u>      OPEN#<id>             marker: this proposal is still open
    USER#<u>      EVENT#<time>#<rand>   audit log entry
    STATE#<key>   STATE                 demo portfolio, conversation history
    CONFIRM#<id>  CONFIRM               a confirm card and its answer
    COUNT#<key>   COUNT                 usage counter

Values are stored as JSON strings in "data", so Decimals stay exact.
With ttl_days set, every item gets an "expires" time and DynamoDB deletes it later.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone

from engine import Proposal, Rules
from engine.audit import AuditEvent

from .codec import proposal_from_dict, proposal_to_dict, rules_from_dict, rules_to_dict
from .memory import OPEN, PENDING


class DynamoStore:
    def __init__(self, table_name: str, ttl_days: float | None = None, region: str | None = None, table=None):
        if table is None:
            import boto3
            table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.table = table
        self.ttl_days = ttl_days
        self.audit = DynamoAuditLog(self)

    # ---- low level ----
    def _expires(self) -> dict:
        return {"expires": int(time.time() + self.ttl_days * 86400)} if self.ttl_days else {}

    def _put(self, pk: str, sk: str, data, **extra) -> None:
        self.table.put_item(Item={"pk": pk, "sk": sk, "data": json.dumps(data, default=str),
                                  **extra, **self._expires()})

    def _get(self, pk: str, sk: str) -> dict | None:
        item = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        if item and "expires" in item and int(item["expires"]) < time.time():
            return None   # expired but not swept yet
        return item

    def _query(self, pk: str, prefix: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix),
                             "ConsistentRead": True}
        while True:
            r = self.table.query(**kwargs)
            items += r.get("Items", [])
            if "LastEvaluatedKey" not in r:
                return items
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    # ---- rules ----
    def get_rules(self, user_id: str) -> Rules | None:
        item = self._get(f"USER#{user_id}", "RULES")
        return rules_from_dict(json.loads(item["data"])) if item else None

    def save_rules(self, user_id: str, rules: Rules) -> None:
        self._put(f"USER#{user_id}", "RULES", rules_to_dict(rules))

    # ---- proposals ----
    def get_proposal(self, proposal_id: str) -> Proposal | None:
        item = self._get(f"PROPOSAL#{proposal_id}", "PROPOSAL")
        return proposal_from_dict(json.loads(item["data"])) if item else None

    def save_proposal(self, proposal: Proposal) -> None:
        self._put(f"PROPOSAL#{proposal.id}", "PROPOSAL", proposal_to_dict(proposal))
        marker = {"pk": f"USER#{proposal.user_id}", "sk": f"OPEN#{proposal.id}"}
        if proposal.status in OPEN:
            self.table.put_item(Item={**marker, **self._expires()})
        else:
            self.table.delete_item(Key=marker)

    def open_proposals(self, user_id: str) -> list[Proposal]:
        found = []
        for m in self._query(f"USER#{user_id}", "OPEN#"):
            p = self.get_proposal(m["sk"].split("#", 1)[1])
            if p and p.status in OPEN:
                found.append(p)
        return found

    # ---- small JSON documents ----
    def get_state(self, key: str) -> dict | None:
        item = self._get(f"STATE#{key}", "STATE")
        return json.loads(item["data"]) if item else None

    def put_state(self, key: str, value: dict) -> None:
        self._put(f"STATE#{key}", "STATE", value)

    # ---- confirm cards ----
    def create_confirm(self, cid: str, owner: str, message: str) -> None:
        self._put(f"CONFIRM#{cid}", "CONFIRM", {"message": message}, owner=owner, answer=PENDING)

    def get_confirm(self, cid: str) -> dict | None:
        item = self._get(f"CONFIRM#{cid}", "CONFIRM")
        if not item:
            return None
        return {"owner": item["owner"], "answer": item["answer"], "message": json.loads(item["data"])["message"]}

    def answer_confirm(self, cid: str, owner: str, approve: bool) -> bool:
        """Answer an open card: only its owner, only once (a conditional write, so it's safe
        even when the waiting request and the answer arrive on different machines)."""
        from botocore.exceptions import ClientError
        try:
            self.table.update_item(
                Key={"pk": f"CONFIRM#{cid}", "sk": "CONFIRM"},
                UpdateExpression="SET answer = :a",
                ConditionExpression="attribute_exists(pk) AND #o = :o AND answer = :p",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":a": "yes" if approve else "no", ":o": owner, ":p": PENDING},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ---- counters ----
    def incr(self, key: str) -> int:
        r = self.table.update_item(
            Key={"pk": f"COUNT#{key}", "sk": "COUNT"},
            UpdateExpression="ADD n :one SET expires = :e",
            ExpressionAttributeValues={":one": 1, ":e": int(time.time() + 3 * 86400)},
            ReturnValues="UPDATED_NEW",
        )
        return int(r["Attributes"]["n"])


class DynamoAuditLog:
    """Same interface as engine.AuditLog, stored in DynamoDB."""

    def __init__(self, store: DynamoStore):
        self.store = store

    def record(self, user_id: str, event: str, proposal_id: str | None = None, **details) -> AuditEvent:
        e = AuditEvent(datetime.now(timezone.utc), user_id, event, proposal_id, details)
        sk = f"EVENT#{e.at.isoformat()}#{secrets.token_hex(3)}"
        self.store._put(f"USER#{user_id}", sk, {"at": e.at.isoformat(), "event": event,
                                                 "proposal_id": proposal_id, "details": details})
        return e

    def events(self, user_id: str | None = None, proposal_id: str | None = None) -> list[AuditEvent]:
        if user_id is None:
            raise ValueError("The DynamoDB audit log is read per user.")
        out = []
        for item in self.store._query(f"USER#{user_id}", "EVENT#"):
            d = json.loads(item["data"])
            if proposal_id is None or d["proposal_id"] == proposal_id:
                out.append(AuditEvent(datetime.fromisoformat(d["at"]), user_id, d["event"],
                                      d["proposal_id"], d.get("details", {})))
        return out
