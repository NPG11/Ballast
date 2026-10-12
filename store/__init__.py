"""Where Ballast keeps rules, proposals, the audit log and session state.

make_store() picks DynamoDB when BALLAST_TABLE is set, otherwise memory.
"""
import os

from .memory import MemoryStore


def make_store():
    table = os.environ.get("BALLAST_TABLE")
    if table:
        from .dynamo import DynamoStore
        ttl = os.environ.get("BALLAST_TTL_DAYS", "14")
        return DynamoStore(table, ttl_days=float(ttl) if ttl else None)
    return MemoryStore()


__all__ = ["MemoryStore", "make_store"]
