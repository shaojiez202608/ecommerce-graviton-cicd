"""AWS Lambda entry points (arm64). Configuration comes from environment variables."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

import boto3

from .service import EVENT_SOURCE, McfBridge, RetryLater, Settings
from .spapi import LiveSpApiClient, MockSpApiClient
from .store import DynamoMockState, DynamoRepository

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

PLACEHOLDER = "REPLACE_ME"


class EventBridgePublisher:
    def __init__(self, bus_name: str, client=None):
        self.bus = bus_name
        self.client = client or boto3.client("events")

    def publish(self, detail_type, detail):
        resp = self.client.put_events(
            Entries=[
                {
                    "Source": EVENT_SOURCE,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail),
                    "EventBusName": self.bus,
                }
            ]
        )
        if resp.get("FailedEntryCount"):
            raise RuntimeError(f"PutEvents failed: {resp['Entries']}")


def build_client(mode: str, table):
    if mode == "mock":
        return MockSpApiClient(
            state=DynamoMockState(table), ship_after_seconds=int(os.environ.get("MOCK_SHIP_AFTER_SECONDS", "60"))
        )
    if mode != "live":
        raise ValueError(f"SPAPI_MODE must be mock or live, got {mode!r}")
    secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SPAPI_SECRET_ARN"])["SecretString"]
    )
    if PLACEHOLDER in secret.values():
        raise RuntimeError("SP-API secret still holds placeholders; refusing live mode")
    return LiveSpApiClient(
        region_key=os.environ.get("SPAPI_REGION", "na"),
        client_id=secret["lwa_client_id"],
        client_secret=secret["lwa_client_secret"],
        refresh_token=secret["lwa_refresh_token"],
    )


@lru_cache(maxsize=1)
def bridge() -> McfBridge:
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    settings = Settings(
        marketplace_ids=json.loads(os.environ["MARKETPLACE_IDS"]),
        default_fulfillment_policy=os.environ.get("FULFILLMENT_POLICY", "FillOrKill"),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
    )
    return McfBridge(
        repo=DynamoRepository(table),
        client=build_client(os.environ.get("SPAPI_MODE", "mock"), table),
        publisher=EventBridgePublisher(os.environ["EVENT_BUS_NAME"]),
        settings=settings,
    )


def order_intake(event, _context):
    """SQS batch handler (ReportBatchItemFailures). Each body is an EventBridge event."""
    failures = []
    for record in event.get("Records", []):
        try:
            envelope = json.loads(record["body"])
            detail = envelope.get("detail", envelope)
            receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
            result = bridge().submit(detail, receive_count=receive_count)
            log.info("order %s -> %s %s", result.order_id, result.state, result.detail)
        except RetryLater as exc:
            log.warning("transient failure, will retry: %s", exc)
            failures.append({"itemIdentifier": record["messageId"]})
        except Exception:
            log.exception("unexpected failure")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def tracking_poller(_event, _context):
    counts = bridge().poll_tracking()
    log.info("tracking poll: %s", counts)
    return counts


def inventory_sync(_event, _context):
    counts = bridge().sync_inventory()
    log.info("inventory sync: %s", counts)
    return counts
