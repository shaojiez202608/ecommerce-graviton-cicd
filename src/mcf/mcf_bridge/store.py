"""Persistence for orders, SKU mappings and mock SP-API state (single DynamoDB table)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol

from .models import MappingStatus, OrderRecord, OrderState


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Repository(Protocol):
    def get_order(self, order_id: str) -> OrderRecord | None: ...
    def create_order_if_absent(self, rec: OrderRecord) -> bool: ...
    def save_order(self, rec: OrderRecord) -> None: ...
    def orders_in_state(self, state: OrderState) -> list[OrderRecord]: ...
    def get_mapping(self, storefront_sku: str, marketplace: str) -> dict | None: ...
    def put_mapping(self, storefront_sku: str, marketplace: str, msku: str) -> None: ...
    def mappings_for(self, marketplace: str) -> list[dict]: ...
    def update_mapping(self, storefront_sku: str, marketplace: str, status: MappingStatus, qty: int | None) -> None: ...


def _order_to_item(rec: OrderRecord) -> dict:
    return {
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
    }


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
        self.items[key] = _order_to_item(rec)
        return True

    def save_order(self, rec):
        rec.updated_at = utcnow()
        self.items[(f"ORDER#{rec.storefront_order_id}", "META")] = _order_to_item(rec)

    def orders_in_state(self, state):
        return [_item_to_order(i) for i in self.items.values() if i.get("gsi1pk") == f"STATE#{state.value}"]

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
        try:
            self.table.put_item(Item=_order_to_item(rec), ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def save_order(self, rec):
        rec.updated_at = utcnow()
        self.table.put_item(Item=_order_to_item(rec))

    def orders_in_state(self, state):
        from boto3.dynamodb.conditions import Key

        out, kwargs = [], {"IndexName": "gsi1", "KeyConditionExpression": Key("gsi1pk").eq(f"STATE#{state.value}")}
        while True:
            resp = self.table.query(**kwargs)
            out.extend(_item_to_order(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def get_mapping(self, storefront_sku, marketplace):
        return self.table.get_item(Key={"pk": f"SKU#{storefront_sku}", "sk": f"MKT#{marketplace}"}).get("Item")

    def put_mapping(self, storefront_sku, marketplace, msku):
        self.table.put_item(Item=_mapping_item(storefront_sku, marketplace, msku))

    def mappings_for(self, marketplace):
        from boto3.dynamodb.conditions import Key

        out, kwargs = [], {"IndexName": "gsi1", "KeyConditionExpression": Key("gsi1pk").eq(f"MAPPING#{marketplace}")}
        while True:
            resp = self.table.query(**kwargs)
            out.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

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
