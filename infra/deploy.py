"""Deploy Ballast to AWS with one command.

    python -m infra.deploy --region us-east-2              # demo portfolio for every visitor
    python -m infra.deploy --region us-east-2 --broker alpaca   # your Alpaca paper account
    python -m infra.deploy --region us-east-2 --destroy    # remove it again (keeps data)

What it creates (all named "ballast..."):
    DynamoDB table   ballast            rules, proposals, audit log, sessions, confirm cards
    Secrets Manager  ballast/alpaca     your Alpaca paper keys (only if ALPACA_KEY/SECRET are set)
    IAM role         ballast-lambda     what the function may do: the table, the secret, Bedrock, logs
    Lambda           ballast            the demo site + MCP server, via the AWS Lambda Web Adapter
    Function URL     https://....lambda-url.<region>.on.aws/   (streaming, public)

Running it again updates everything in place. The MCP token is kept in infra/.deploy.json
(git-ignored) so it stays the same between deploys.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "infra" / "build"
STATE_FILE = ROOT / "infra" / ".deploy.json"
CODE_DIRS = ["adapters", "agent", "engine", "mcp_server", "service", "store", "web"]
NAME = "ballast"
SECRET_NAME = "ballast/alpaca"
RUNTIME = "python3.12"
WEB_ADAPTER = "arn:aws:lambda:{region}:753240598075:layer:LambdaAdapterLayerX86:30"

RUN_SH = """#!/bin/bash
PATH=$PATH:$LAMBDA_TASK_ROOT/bin \\
    PYTHONPATH=$PYTHONPATH:/opt/python:$LAMBDA_RUNTIME_DIR \\
    exec python -m uvicorn --host 127.0.0.1 --port=$PORT web.server:app
"""


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


# ---------------- package ----------------

def build_zip() -> bytes:
    """Linux packages + Ballast's code + the startup script, zipped in memory."""
    pkg = BUILD / "pkg"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)
    say("Installing Linux packages for Lambda (uv)...")
    try:
        import uv  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "uv"], check=True)
    subprocess.run([sys.executable, "-m", "uv", "pip", "install", "--quiet", "--target", str(pkg),
                    "--python-platform", "x86_64-manylinux2014", "--python-version", "3.12",
                    "--only-binary", ":all:", "-r", str(ROOT / "requirements-lambda.txt")], check=True)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(pkg.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(pkg).as_posix())
        for d in CODE_DIRS:
            for path in sorted((ROOT / d).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    z.write(path, path.relative_to(ROOT).as_posix())
        info = zipfile.ZipInfo("run.sh")
        info.external_attr = 0o100755 << 16           # must be executable on Linux
        z.writestr(info, RUN_SH.replace("\r\n", "\n"))
    data = buf.getvalue()
    say(f"Package: {len(data) / 1e6:.1f} MB")
    if len(data) > 50_000_000:
        raise SystemExit("The package is over Lambda's 50 MB direct-upload limit.")
    return data


# ---------------- resources ----------------

def ensure_table(ddb) -> str:
    try:
        arn = ddb.describe_table(TableName=NAME)["Table"]["TableArn"]
        say(f"DynamoDB table '{NAME}' exists")
    except ddb.exceptions.ResourceNotFoundException:
        say(f"Creating DynamoDB table '{NAME}' (on-demand billing)...")
        arn = ddb.create_table(
            TableName=NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
        )["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=NAME)
    ttl = ddb.describe_time_to_live(TableName=NAME)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
        ddb.update_time_to_live(TableName=NAME,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires"})
        say("Turned on automatic expiry for old demo data")
    return arn


def ensure_secret(sm, required: bool) -> str | None:
    key, secret = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    value = json.dumps({"key": key, "secret": secret}) if key and secret else None
    try:
        arn = sm.describe_secret(SecretId=SECRET_NAME)["ARN"]
        if value:
            sm.put_secret_value(SecretId=SECRET_NAME, SecretString=value)
            say(f"Updated Alpaca keys in Secrets Manager ({SECRET_NAME})")
        else:
            say(f"Using the Alpaca keys already in Secrets Manager ({SECRET_NAME})")
        return arn
    except sm.exceptions.ResourceNotFoundException:
        if not value:
            if required:
                raise SystemExit("--broker alpaca needs your keys: set ALPACA_KEY and ALPACA_SECRET in this terminal.")
            say("No Alpaca keys set, so skipping Secrets Manager (the demo portfolio doesn't need them)")
            return None
        say(f"Storing your Alpaca paper keys in Secrets Manager ({SECRET_NAME})...")
        return sm.create_secret(Name=SECRET_NAME, SecretString=value,
                                Description="Alpaca PAPER trading keys for Ballast")["ARN"]


def ensure_role(iam, table_arn: str, secret_arn: str | None) -> tuple[str, bool]:
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                      "Principal": {"Service": "lambda.amazonaws.com"}}]}
    statements = [
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
        {"Sid": "Table", "Effect": "Allow",
         "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                    "dynamodb:Query"], "Resource": table_arn},
        {"Sid": "Bedrock", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
    ]
    if secret_arn:
        statements.append({"Sid": "AlpacaKeys", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                           "Resource": secret_arn})
    created = False
    try:
        arn = iam.get_role(RoleName=f"{NAME}-lambda")["Role"]["Arn"]
        say(f"IAM role '{NAME}-lambda' exists")
    except iam.exceptions.NoSuchEntityException:
        say(f"Creating IAM role '{NAME}-lambda'...")
        arn = iam.create_role(RoleName=f"{NAME}-lambda", AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Ballast Lambda: its DynamoDB table, Alpaca secret, Bedrock, logs")["Role"]["Arn"]
        created = True
    iam.put_role_policy(RoleName=f"{NAME}-lambda", PolicyName=f"{NAME}-access",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}))
    return arn, created


def ensure_function(lam, region: str, role_arn: str, code: bytes, env: dict, new_role: bool) -> None:
    config = dict(Runtime=RUNTIME, Role=role_arn, Handler="run.sh", Timeout=300, MemorySize=1024,
                  Layers=[WEB_ADAPTER.format(region=region)], Environment={"Variables": env},
                  Description="Ballast: Alexa+-style portfolio guardian (demo site + MCP server)")
    try:
        lam.get_function(FunctionName=NAME)
        exists = True
    except lam.exceptions.ResourceNotFoundException:
        exists = False

    if exists:
        say("Uploading new code...")
        lam.update_function_code(FunctionName=NAME, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        lam.update_function_configuration(FunctionName=NAME, **config)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        return

    say("Creating the Lambda function...")
    for attempt in range(12):   # a brand-new role takes a few seconds before Lambda can use it
        try:
            lam.create_function(FunctionName=NAME, Code={"ZipFile": code}, Architectures=["x86_64"], **config)
            break
        except lam.exceptions.InvalidParameterValueException as e:
            if "role" not in str(e).lower() or attempt == 11:
                raise
            time.sleep(5 if new_role else 2)
    lam.get_waiter("function_active_v2").wait(FunctionName=NAME)


def ensure_url(lam) -> str:
    try:
        url = lam.get_function_url_config(FunctionName=NAME)["FunctionUrl"].rstrip("/") + "/"
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=NAME, AuthType="NONE",
                                             InvokeMode="RESPONSE_STREAM")["FunctionUrl"]
    url = url.rstrip("/") + "/"
    # Let anyone open the site (the MCP endpoint still needs its token).
    grants = [dict(StatementId="public-url", Action="lambda:InvokeFunctionUrl", FunctionUrlAuthType="NONE"),
              dict(StatementId="public-url-invoke", Action="lambda:InvokeFunction", InvokedViaFunctionUrl=True)]
    for g in grants:
        try:
            lam.add_permission(FunctionName=NAME, Principal="*", **g)
        except lam.exceptions.ResourceConflictException:
            pass                                   # already granted
        except Exception as e:                     # older boto3 without InvokedViaFunctionUrl
            if g["StatementId"] == "public-url":
                raise
            say(f"(skipped the second URL permission: {type(e).__name__})")
    return url


def wait_until_up(url: str) -> bool:
    say("Waiting for the site to answer (the first start takes a few seconds)...")
    for _ in range(20):
        try:
            with urllib.request.urlopen(url + "healthz", timeout=30) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


# ---------------- main ----------------

def deploy(args, session) -> dict:
    region = session.region_name
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    token = state.get("mcp_token") or secrets.token_urlsafe(24)

    print(f"\nDeploying Ballast to {region} (broker: {args.broker})\n")
    table_arn = ensure_table(session.client("dynamodb"))
    secret_arn = ensure_secret(session.client("secretsmanager"), required=args.broker == "alpaca")
    role_arn, new_role = ensure_role(session.client("iam"), table_arn, secret_arn)
    code = build_zip() if args.code is None else Path(args.code).read_bytes()

    env = {
        "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
        "AWS_LWA_INVOKE_MODE": "response_stream",
        "AWS_LWA_READINESS_CHECK_PATH": "/healthz",
        "PORT": "8080",
        "BALLAST_TABLE": NAME,
        "BALLAST_TTL_DAYS": "14",
        "BALLAST_BROKER": "alpaca" if args.broker == "alpaca" else "fake",
        "BALLAST_LLM": args.llm,
        "BALLAST_MODEL": args.model,
        "BALLAST_DAILY_TURN_LIMIT": str(args.daily_limit),
        "BALLAST_MCP_TOKEN": token,
    }
    if secret_arn:
        env["ALPACA_SECRET_ID"] = SECRET_NAME
    lam = session.client("lambda")
    ensure_function(lam, region, role_arn, code, env, new_role)
    url = ensure_url(lam)

    state.update({"region": region, "url": url, "mcp_token": token})
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return {"url": url, "mcp_url": url + "mcp", "token": token, "region": region}


def destroy(args, session) -> None:
    print(f"\nRemoving Ballast from {session.region_name}\n")
    lam, iam = session.client("lambda"), session.client("iam")
    for call, kw in [(lam.delete_function_url_config, {}), (lam.delete_function, {})]:
        try:
            call(FunctionName=NAME, **kw)
        except lam.exceptions.ResourceNotFoundException:
            pass
    say("Lambda function and URL removed")
    try:
        iam.delete_role_policy(RoleName=f"{NAME}-lambda", PolicyName=f"{NAME}-access")
        iam.delete_role(RoleName=f"{NAME}-lambda")
        say("IAM role removed")
    except iam.exceptions.NoSuchEntityException:
        pass
    if args.delete_data:
        ddb, sm = session.client("dynamodb"), session.client("secretsmanager")
        try:
            ddb.delete_table(TableName=NAME)
            say("DynamoDB table deleted")
        except ddb.exceptions.ResourceNotFoundException:
            pass
        try:
            sm.delete_secret(SecretId=SECRET_NAME, ForceDeleteWithoutRecovery=True)
            say("Alpaca secret deleted")
        except sm.exceptions.ResourceNotFoundException:
            pass
    else:
        say("Kept the DynamoDB table and the Alpaca secret (add --delete-data to remove them too)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deploy Ballast to AWS Lambda.")
    ap.add_argument("--region", help="AWS region, e.g. us-east-2 (default: your AWS CLI region)")
    ap.add_argument("--broker", choices=["demo", "alpaca"], default="demo",
                    help="demo: every visitor gets their own demo portfolio; alpaca: your paper account")
    ap.add_argument("--model", default=os.environ.get("BALLAST_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"))
    ap.add_argument("--llm", choices=["bedrock", "offline"], default="bedrock")
    ap.add_argument("--daily-limit", type=int, default=300, help="max chat turns per day for the whole site")
    ap.add_argument("--code", help=argparse.SUPPRESS)          # tests: use a prebuilt zip
    ap.add_argument("--destroy", action="store_true", help="remove the function, URL and role")
    ap.add_argument("--delete-data", action="store_true", help="with --destroy: also delete the table and secret")
    args = ap.parse_args(argv)

    import boto3
    session = boto3.session.Session(region_name=args.region)
    if not session.region_name:
        raise SystemExit("Pick a region: python -m infra.deploy --region us-east-2")

    if args.destroy:
        destroy(args, session)
        return None

    out = deploy(args, session)
    up = args.code is not None or wait_until_up(out["url"])
    print("\nDone." if up else "\nDeployed, but the site didn't answer yet. Give it a minute, then open the link.")
    print(f"\n  Demo site:   {out['url']}")
    print(f"  MCP server:  {out['mcp_url']}")
    print(f"  MCP token:   {out['token']}   (send as 'Authorization: Bearer <token>')")
    print(f"\n  Logs:  aws logs tail /aws/lambda/{NAME} --follow --region {out['region']}\n")
    return out


if __name__ == "__main__":
    main()
