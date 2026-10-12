"""Run the deploy script against simulated AWS (moto): every resource is created and wired up."""
import io
import json
import zipfile

import pytest

moto = pytest.importorskip("moto")


@pytest.fixture
def aws(monkeypatch, tmp_path):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_DEFAULT_REGION": "us-east-2", "ALPACA_KEY": "PKTEST", "ALPACA_SECRET": "sek"}.items():
        monkeypatch.setenv(k, v)
    import infra.deploy as d
    monkeypatch.setattr(d, "STATE_FILE", tmp_path / ".deploy.json")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("run.sh", "#!/bin/bash\n")
    code = tmp_path / "code.zip"
    code.write_bytes(zbuf.getvalue())
    from moto import mock_aws
    with mock_aws():
        import boto3
        # moto doesn't have AWS's public Lambda Web Adapter layer, so publish a stand-in.
        layer = boto3.client("lambda", region_name="us-east-2").publish_layer_version(
            LayerName="LambdaAdapterLayerX86", Content={"ZipFile": zbuf.getvalue()})
        monkeypatch.setattr(d, "WEB_ADAPTER", layer["LayerVersionArn"])
        yield d, str(code)


def test_deploy_creates_and_wires_everything(aws):
    import boto3
    d, code = aws
    out = d.main(["--region", "us-east-2", "--broker", "alpaca", "--code", code])
    assert out["url"].startswith("https://") and out["mcp_url"].endswith("/mcp")

    fn = boto3.client("lambda", region_name="us-east-2").get_function(FunctionName="ballast")["Configuration"]
    env = fn["Environment"]["Variables"]
    assert fn["Handler"] == "run.sh" and fn["Runtime"] == "python3.12"
    assert env["BALLAST_TABLE"] == "ballast" and env["BALLAST_BROKER"] == "alpaca"
    assert env["ALPACA_SECRET_ID"] == "ballast/alpaca" and env["AWS_LWA_INVOKE_MODE"] == "response_stream"
    assert env["BALLAST_MCP_TOKEN"] == out["token"]
    assert "LambdaAdapterLayerX86" in fn["Layers"][0]["Arn"]

    url_cfg = boto3.client("lambda", region_name="us-east-2").get_function_url_config(FunctionName="ballast")
    assert url_cfg["InvokeMode"] == "RESPONSE_STREAM" and url_cfg["AuthType"] == "NONE"

    secret = boto3.client("secretsmanager", region_name="us-east-2").get_secret_value(SecretId="ballast/alpaca")
    assert json.loads(secret["SecretString"]) == {"key": "PKTEST", "secret": "sek"}

    policy = boto3.client("iam").get_role_policy(RoleName="ballast-lambda", PolicyName="ballast-access")["PolicyDocument"]
    policy = json.loads(policy) if isinstance(policy, str) else policy
    sids = {s["Sid"] for s in policy["Statement"]}
    assert sids == {"Logs", "Table", "Bedrock", "AlpacaKeys"}

    ttl = boto3.client("dynamodb", region_name="us-east-2").describe_time_to_live(TableName="ballast")
    assert ttl["TimeToLiveDescription"]["AttributeName"] == "expires"


def test_redeploy_updates_in_place_and_keeps_the_token(aws):
    d, code = aws
    first = d.main(["--region", "us-east-2", "--code", code])
    second = d.main(["--region", "us-east-2", "--code", code])
    assert first["url"] == second["url"] and first["token"] == second["token"]


def test_alpaca_mode_without_keys_stops_clearly(aws, monkeypatch):
    d, code = aws
    monkeypatch.delenv("ALPACA_KEY")
    with pytest.raises(SystemExit, match="ALPACA_KEY"):
        d.main(["--region", "us-east-2", "--broker", "alpaca", "--code", code])


def test_destroy_removes_function_and_role(aws):
    import boto3
    d, code = aws
    d.main(["--region", "us-east-2", "--code", code])
    d.main(["--region", "us-east-2", "--destroy"])
    names = [f["FunctionName"] for f in boto3.client("lambda", region_name="us-east-2").list_functions()["Functions"]]
    assert "ballast" not in names
    assert boto3.client("dynamodb", region_name="us-east-2").describe_table(TableName="ballast")   # data kept
