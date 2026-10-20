"""Lambda handlers + DynamoDB repository against moto (in-process AWS mock)."""

import json
import os

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "gsi1pk", "gsi1sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{"IndexName": "gsi1", "Projection": {"ProjectionType": "ALL"}, "KeySchema": [
                {"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}]}],
        )
        events = boto3.client("events")
        events.create_event_bus(Name="bus")
        logs = boto3.client("logs")
        logs.create_log_group(logGroupName="/test/out")
        events.put_rule(Name="all", EventBusName="bus", EventPattern=json.dumps({"source": ["mcf.bridge"]}))
        monkeypatch.setenv("TABLE_NAME", "t")
        monkeypatch.setenv("EVENT_BUS_NAME", "bus")
        monkeypatch.setenv("SPAPI_MODE", "mock")
        monkeypatch.setenv("MARKETPLACE_IDS", json.dumps({"US": "ATVPDKIKX0DER"}))
        monkeypatch.setenv("MOCK_SHIP_AFTER_SECONDS", "0")
        from mcf_bridge import handlers
        handlers.bridge.cache_clear()
        yield handlers
        handlers.bridge.cache_clear()


def sqs_record(detail, msg_id="m1", receive_count="1"):
    return {"messageId": msg_id, "attributes": {"ApproximateReceiveCount": receive_count},
            "body": json.dumps({"source": "storefront.orders", "detail-type": "Order Paid", "detail": detail})}


def order(order_id, name="Alex Example"):
    return {"orderId": order_id, "marketplace": "US", "placedAt": "2026-10-20T08:00:00Z",
            "shippingAddress": {"name": name, "addressLine1": "1 Example St", "postalCode": "98000", "countryCode": "US"},
            "lines": [{"sku": "SKU-A", "quantity": 1}]}


def test_end_to_end_with_dynamodb_state(aws):
    handlers = aws
    repo = handlers.bridge().repo
    repo.put_mapping("SKU-A", "US", "MSKU-A")
    out = handlers.order_intake({"Records": [sqs_record(order("A1")), sqs_record(order("A2", "FAIL_ALWAYS"), "m2")]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    assert repo.get_order("A1").state.value == "SUBMITTED"
    assert repo.get_order("A2").state.value == "RECEIVED"
    out = handlers.order_intake({"Records": [sqs_record(order("A2", "FAIL_ALWAYS"), "m2", "3")]}, None)
    assert out["batchItemFailures"] and repo.get_order("A2").state.value == "EXCEPTION"
    assert handlers.tracking_poller({}, None)["completed"] == 1
    assert repo.get_order("A1").state.value == "COMPLETE"
    assert handlers.inventory_sync({}, None) == {"skus": 1, "invalid": 0, "out_of_stock": 0}
    # mock SP-API state persisted in the table (survives cold starts)
    items = boto3.resource("dynamodb").Table("t").scan()["Items"]
    assert any(i["pk"].startswith("MOCK#order#SF-A1") for i in items)


def test_live_mode_refuses_placeholder_secret(aws, monkeypatch):
    handlers = aws
    sm = boto3.client("secretsmanager")
    arn = sm.create_secret(Name="s", SecretString=json.dumps(
        {"lwa_client_id": "REPLACE_ME", "lwa_client_secret": "REPLACE_ME", "lwa_refresh_token": "REPLACE_ME"}))["ARN"]
    monkeypatch.setenv("SPAPI_MODE", "live")
    monkeypatch.setenv("SPAPI_SECRET_ARN", arn)
    handlers.bridge.cache_clear()
    with pytest.raises(RuntimeError, match="placeholders"):
        handlers.bridge()


def test_invalid_mode_rejected(aws, monkeypatch):
    monkeypatch.setenv("SPAPI_MODE", "prod")
    aws.bridge.cache_clear()
    with pytest.raises(ValueError):
        aws.bridge()
    assert os.environ["TABLE_NAME"] == "t"
