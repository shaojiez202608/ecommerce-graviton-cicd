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
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[
                {"IndexName": ix, "Projection": {"ProjectionType": "ALL"}, "KeySchema": [
                    {"AttributeName": f"{ix}pk", "KeyType": "HASH"}, {"AttributeName": f"{ix}sk", "KeyType": "RANGE"}]}
                for ix in ("gsi1", "gsi2")],
        )
        events = boto3.client("events")
        events.create_event_bus(Name="bus")
        logs = boto3.client("logs")
        logs.create_log_group(logGroupName="/test/out")
        events.put_rule(Name="all", EventBusName="bus", EventPattern=json.dumps({"source": ["mcf.bridge"]}))
        monkeypatch.setenv("TABLE_NAME", "t")
        monkeypatch.setenv("EVENT_BUS_NAME", "bus")
        monkeypatch.setenv("SPAPI_MODE", "mock")
        monkeypatch.setenv("MARKETPLACES", json.dumps({"US": {"id": "ATVPDKIKX0DER", "region": "na"},
                                                        "DE": {"id": "A1PA6795UKMFR9", "region": "eu"}}))
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
    assert repo.get_order("A1").pending_events == []  # every event reached the bus
    # mock SP-API state persisted in the table (survives cold starts)
    items = boto3.resource("dynamodb").Table("t").scan()["Items"]
    assert any(i["pk"].startswith("MOCK#order#SF-A1") for i in items)


def test_live_mode_refuses_placeholder_secret(aws, monkeypatch):
    handlers = aws
    sm = boto3.client("secretsmanager")
    arn = sm.create_secret(Name="s", SecretString=json.dumps(
        {"lwa_client_id": "REPLACE_ME", "lwa_client_secret": "REPLACE_ME", "lwa_refresh_token_na": "REPLACE_ME",
         "lwa_refresh_token_eu": "REPLACE_ME"}))["ARN"]
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


def test_cancel_event_and_regional_routing(aws, monkeypatch):
    handlers = aws
    handlers.bridge().repo.put_mapping("SKU-A", "DE", "MSKU-A-DE")
    monkeypatch.setenv("MOCK_SHIP_AFTER_SECONDS", "3600")
    handlers.bridge.cache_clear()
    repo = handlers.bridge().repo
    de = dict(order("D1"), marketplace="DE")
    assert handlers.order_intake({"Records": [sqs_record(de)]}, None) == {"batchItemFailures": []}
    clients = handlers.bridge().clients
    assert set(clients) == {"na", "eu"}
    mock = boto3.resource("dynamodb").Table("t").get_item(Key={"pk": "MOCK#order#SF-D1", "sk": "MOCK"})["Item"]
    assert json.loads(mock["v"])["region"] == "eu"
    cancel = {"messageId": "c1", "attributes": {"ApproximateReceiveCount": "1"},
              "body": json.dumps({"detail-type": "Order Cancelled", "detail": {"orderId": "D1"}})}
    assert handlers.order_intake({"Records": [cancel]}, None) == {"batchItemFailures": []}
    assert repo.get_order("D1").state.value == "CANCELLED"


def test_live_mode_needs_a_token_per_region(aws, monkeypatch):
    handlers = aws
    sm = boto3.client("secretsmanager")
    arn = sm.create_secret(Name="s2", SecretString=json.dumps(
        {"lwa_client_id": "id", "lwa_client_secret": "secret", "lwa_refresh_token_na": "token"}))["ARN"]
    monkeypatch.setenv("SPAPI_MODE", "live")
    monkeypatch.setenv("SPAPI_SECRET_ARN", arn)
    handlers.bridge.cache_clear()
    with pytest.raises(RuntimeError, match="lwa_refresh_token_eu"):
        handlers.bridge()


def test_redrive_sets_retry_and_the_order_recovers(aws):
    handlers = aws
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from mcf_redrive import redrive

    repo = handlers.bridge().repo
    repo.put_mapping("SKU-A", "US", "MSKU-A")
    for n in ("1", "2", "3"):
        handlers.order_intake({"Records": [sqs_record(order("R1", "FAIL_FIRST_3"), "m1", n)]}, None)
    assert repo.get_order("R1").state.value == "EXCEPTION"
    sqs = boto3.client("sqs")
    q = sqs.create_queue(QueueName="q")["QueueUrl"]
    dlq = sqs.create_queue(QueueName="dlq")["QueueUrl"]
    sqs.send_message(QueueUrl=dlq, MessageBody=sqs_record(order("R1", "FAIL_FIRST_3"))["body"])
    assert redrive(sqs, dlq, q) == ["R1"]
    msg = sqs.receive_message(QueueUrl=q)["Messages"][0]
    assert json.loads(msg["Body"])["detail"]["retry"] is True
    out = handlers.order_intake({"Records": [{"messageId": "r", "attributes": {"ApproximateReceiveCount": "1"},
                                              "body": msg["Body"]}]}, None)
    assert out == {"batchItemFailures": []} and repo.get_order("R1").state.value == "SUBMITTED"
    assert "Messages" not in sqs.receive_message(QueueUrl=dlq, WaitTimeSeconds=0)
