"""Alpaca keys: from environment variables locally, from AWS Secrets Manager when deployed."""
from __future__ import annotations

import json
import os
from functools import lru_cache


class MissingKeys(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_alpaca_keys() -> tuple[str, str]:
    key, secret = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    if key and secret:
        return key, secret
    secret_id = os.environ.get("ALPACA_SECRET_ID")
    if secret_id:
        import boto3
        value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
        data = json.loads(value)
        return data["key"], data["secret"]
    raise MissingKeys("Set ALPACA_KEY and ALPACA_SECRET (or ALPACA_SECRET_ID for Secrets Manager).")
