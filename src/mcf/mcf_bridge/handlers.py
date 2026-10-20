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


def marketplaces() -> dict[str, dict]:
    """MARKETPLACES = {"US": {"id": "ATVPDKIKX0DER", "region": "na"}, ...}"""
    return json.loads(os.environ["MARKETPLACES"])


def build_clients(mode: str, table) -> dict:
    """One SP-API client per endpoint region used by the configured marketplaces."""
    regions = sorted({m["region"] for m in marketplaces().values()})
    if mode == "mock":
        state = DynamoMockState(table)
        ship_after = int(os.environ.get("MOCK_SHIP_AFTER_SECONDS", "60"))
        return {r: MockSpApiClient(state=state, ship_after_seconds=ship_after, region=r) for r in regions}
    if mode != "live":
        raise ValueError(f"SPAPI_MODE must be mock or live, got {mode!r}")
    secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SPAPI_SECRET_ARN"])["SecretString"]
    )
    if PLACEHOLDER in secret.values():
        raise RuntimeError("SP-API secret still holds placeholders; refusing live mode")
    timeout = int(os.environ.get("SPAPI_HTTP_TIMEOUT_SECONDS", "20"))
    clients = {}
    for region in regions:
        token = secret.get(f"lwa_refresh_token_{region}")
        if not token:
            raise RuntimeError(f"SP-API secret has no lwa_refresh_token_{region}; refusing live mode")
        clients[region] = LiveSpApiClient(
            region_key=region,
            client_id=secret["lwa_client_id"],
            client_secret=secret["lwa_client_secret"],
            refresh_token=token,
            timeout_seconds=timeout,
        )
    return clients


@lru_cache(maxsize=1)
def bridge() -> McfBridge:
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    markets = marketplaces()
    settings = Settings(
        marketplace_ids={k: v["id"] for k, v in markets.items()},
        marketplace_regions={k: v["region"] for k, v in markets.items()},
        default_fulfillment_policy=os.environ.get("FULFILLMENT_POLICY", "FillOrKill"),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
        stuck_received_minutes=int(os.environ.get("STUCK_RECEIVED_MINUTES", "0")),
    )
    return McfBridge(
        repo=DynamoRepository(table),
        client=build_clients(os.environ.get("SPAPI_MODE", "mock"), table),
        publisher=EventBridgePublisher(os.environ["EVENT_BUS_NAME"]),
        settings=settings,
    )


def order_intake(event, _context):
    """SQS batch handler (ReportBatchItemFailures). Each body is an EventBridge event:
    `Order Paid` -> submit to MCF, `Order Cancelled` -> cancel."""
    failures = []
    for record in event.get("Records", []):
        try:
            envelope = json.loads(record["body"])
            detail = envelope.get("detail", envelope)
            detail_type = envelope.get("detail-type", "Order Paid")
            receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
            if detail_type == "Order Cancelled":
                result = bridge().cancel(detail)
            else:
                result = bridge().submit(detail, receive_count=receive_count)
            log.info("%s %s -> %s %s", detail_type, result.order_id, result.state, result.detail)
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
