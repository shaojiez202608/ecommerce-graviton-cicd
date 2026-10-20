"""Persistence for orders, SKU mappings and mock SP-API state (single DynamoDB table).

Order items carry a ``version`` attribute; every save is conditional on the version that was read
(optimistic locking), so the intake and tracking functions cannot overwrite each other's changes.
Orders with undelivered events are indexed in the sparse ``gsi2`` index (``gsi2pk = OUTBOX``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol

from .models import MappingStatus, OrderRecord, OrderState

OUTBOX = "OUTBOX"


class ConcurrentUpdate(Exception):
    """The order changed since it was read; re-read and retry."""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Repository(Protocol):
    def get_order(self, order_id: str) -> OrderRecord | None: ...
    def create_order_if_absent(self, rec: OrderRecord) -> bool: ...
    def save_order(self, rec: OrderRecord) -> None: ...
    def orders_in_state(self, state: OrderState) -> list[OrderRecord]: ...
    def orders_with_pending_events(self) -> list[OrderRecord]: ...
    def get_mapping(self, storefront_sku: str, marketplace: str) -> dict | None: ...
    def put_mapping(self, storefront_sku: str, marketplace: str, msku: str) -> None: ...
    def mappings_for(self, marketplace: str) -> list[dict]: ...
    def update_mapping(self, storefront_sku: str, marketplace: str, status: MappingStatus, qty: int | None) -> None: ...


def _order_to_item(rec: OrderRecord) -> dict:
    item = {
        "pk": f"ORDER#{rec.storefront_order_id}",
        "sk": "META",
        "gsi1pk": f"STATE#{rec.state.value}",
        "gsi1sk": rec.updated_at or utcnow(),
        "orderId": rec.storefront_order_id,
        "state": rec.state.value,
        "sfoId": rec.seller_fulfillment_order_id,
        "marketplace": rec.marketplace,
        "attempts": rec.attempts,
        "lastError": rec.last_error,
        "packages": json.dumps(rec.packages),
        "updatedAt": rec.updated_at or utcnow(),
        "version": rec.version,
        "pendingEvents": json.dumps(rec.pending_events),
        "eventSeq": rec.event_seq,
        "mcfCheck": rec.mcf_check,
    }
    if rec.pending_events:  # sparse index: only orders with undelivered events
        item["gsi2pk"] = OUTBOX
        item["gsi2sk"] = item["updatedAt"]
    return item


def _item_to_order(item: dict) -> OrderRecord:
    return OrderRecord(
        storefront_order_id=item["orderId"],
        state=OrderState(item["state"]),
        seller_fulfillment_order_id=item["sfoId"],
        marketplace=item["marketplace"],
        attempts=int(item.get("attempts", 0)),
        last_error=item.get("lastError", ""),
        packages=json.loads(item.get("packages") or "[]"),
        updated_at=item.get("updatedAt", ""),
        version=int(item.get("version", 0)),
        pending_events=json.loads(item.get("pendingEvents") or "[]"),
        event_seq=int(item.get("eventSeq", 0)),
        mcf_check=bool(item.get("mcfCheck", False)),
    )


class InMemoryRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def get_order(self, order_id):
        item = self.items.get((f"ORDER#{order_id}", "META"))
        return _item_to_order(item) if item else None

    def create_order_if_absent(self, rec):
        key = (f"ORDER#{rec.storefront_order_id}", "META")
        if key in self.items:
            return False
        rec.updated_at = utcnow()
        rec.version = 1
        self.items[key] = _order_to_item(rec)
        return True

    def save_order(self, rec):
        key = (f"ORDER#{rec.storefront_order_id}", "META")
        current = self.items.get(key)
        if current is not None and int(current.get("version", 0)) != rec.version:
            raise ConcurrentUpdate(rec.storefront_order_id)
        rec.updated_at = utcnow()
        rec.version += 1
        self.items[key] = _order_to_item(rec)

    def orders_in_state(self, state):
        return [_item_to_order(i) for i in self.items.values() if i.get("gsi1pk") == f"STATE#{state.value}"]

    def orders_with_pending_events(self):
        return [_item_to_order(i) for i in self.items.values() if i.get("gsi2pk") == OUTBOX]

    def get_mapping(self, storefront_sku, marketplace):
        return self.items.get((f"SKU#{storefront_sku}", f"MKT#{marketplace}"))

    def put_mapping(self, storefront_sku, marketplace, msku):
        self.items[(f"SKU#{storefront_sku}", f"MKT#{marketplace}")] = _mapping_item(storefront_sku, marketplace, msku)

    def mappings_for(self, marketplace):
        return [i for i in self.items.values() if i.get("gsi1pk") == f"MAPPING#{marketplace}"]

    def update_mapping(self, storefront_sku, marketplace, status, qty):
        item = self.items[(f"SKU#{storefront_sku}", f"MKT#{marketplace}")]
        item.update({"status": status.value, "checkedAt": utcnow()})
        if qty is not None:
            item["fulfillableQuantity"] = qty


def _mapping_item(storefront_sku: str, marketplace: str, msku: str) -> dict:
    return {
        "pk": f"SKU#{storefront_sku}",
        "sk": f"MKT#{marketplace}",
        "gsi1pk": f"MAPPING#{marketplace}",
        "gsi1sk": storefront_sku,
        "storefrontSku": storefront_sku,
        "marketplace": marketplace,
        "msku": msku,
        "status": MappingStatus.UNVERIFIED.value,
    }


class DynamoRepository:
    """boto3 implementation. ``table`` is a boto3 ``Table`` resource."""

    def __init__(self, table) -> None:
        self.table = table

    def get_order(self, order_id):
        item = self.table.get_item(Key={"pk": f"ORDER#{order_id}", "sk": "META"}, ConsistentRead=True).get("Item")
        return _item_to_order(item) if item else None

    def create_order_if_absent(self, rec):
        from botocore.exceptions import ClientError

        rec.updated_at = utcnow()
        rec.version = 1
        try:
            self.table.put_item(Item=_order_to_item(rec), ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def save_order(self, rec):
        from botocore.exceptions import ClientError

        expected = rec.version
        rec.updated_at = utcnow()
        rec.version = expected + 1
        try:
            self.table.put_item(
                Item=_order_to_item(rec),
                ConditionExpression="attribute_not_exists(pk) OR attribute_not_exists(version) OR version = :v",
                ExpressionAttributeValues={":v": expected},
            )
        except ClientError as exc:
            rec.version = expected
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConcurrentUpdate(rec.storefront_order_id) from exc
            raise

    def _query(self, index: str, key: str, value: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        out, kwargs = [], {"IndexName": index, "KeyConditionExpression": Key(key).eq(value)}
        while True:
            resp = self.table.query(**kwargs)
            out.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def orders_in_state(self, state):
        # GSI reads are eventually consistent; callers re-read the item (ConsistentRead) before acting.
        return [_item_to_order(i) for i in self._query("gsi1", "gsi1pk", f"STATE#{state.value}")]

    def orders_with_pending_events(self):
        return [_item_to_order(i) for i in self._query("gsi2", "gsi2pk", OUTBOX)]

    def get_mapping(self, storefront_sku, marketplace):
        return self.table.get_item(Key={"pk": f"SKU#{storefront_sku}", "sk": f"MKT#{marketplace}"}).get("Item")

    def put_mapping(self, storefront_sku, marketplace, msku):
        self.table.put_item(Item=_mapping_item(storefront_sku, marketplace, msku))

    def mappings_for(self, marketplace):
        return self._query("gsi1", "gsi1pk", f"MAPPING#{marketplace}")

    def update_mapping(self, storefront_sku, marketplace, status, qty):
        expr, values = "SET #s = :s, checkedAt = :c", {":s": status.value, ":c": utcnow()}
        if qty is not None:
            expr += ", fulfillableQuantity = :q"
            values[":q"] = qty
        self.table.update_item(
            Key={"pk": f"SKU#{storefront_sku}", "sk": f"MKT#{marketplace}"},
            UpdateExpression=expr,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )


class DynamoMockState:
    """Persists mock SP-API state in the same table so the deployed mock survives cold starts."""

    def __init__(self, table) -> None:
        self.table = table

    def get(self, key):
        item = self.table.get_item(Key={"pk": f"MOCK#{key}", "sk": "MOCK"}, ConsistentRead=True).get("Item")
        return json.loads(item["v"]) if item else None

    def put(self, key, value):
        self.table.put_item(Item={"pk": f"MOCK#{key}", "sk": "MOCK", "v": json.dumps(value)})
