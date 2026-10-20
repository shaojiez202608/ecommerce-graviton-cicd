"""Business logic: order-paid -> createFulfillmentOrder -> tracking -> storefront events.

Pure Python; AWS access is injected (repository, SP-API client, event publisher).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .models import (
    SHIPPING_SPEED,
    TERMINAL_STATES,
    MappingStatus,
    OrderRecord,
    OrderState,
    PaidOrder,
    ValidationError,
)
from .spapi import SpApiClient, SpApiError
from .store import Repository

log = logging.getLogger(__name__)

EVENT_SOURCE = "mcf.bridge"
INVENTORY_BATCH = 50  # getInventorySummaries sellerSkus maxItems in the official model


class Publisher(Protocol):
    def publish(self, detail_type: str, detail: dict) -> None: ...


class ListPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, detail_type, detail):
        self.events.append((detail_type, detail))


@dataclass
class Settings:
    marketplace_ids: dict[str, str]  # logical key -> SP-API marketplaceId
    default_fulfillment_policy: str = "FillOrKill"
    max_attempts: int = 3
    notification_emails: list[str] = field(default_factory=list)


class RetryLater(Exception):
    """Transient failure; the queue will redeliver the message."""


@dataclass
class SubmitResult:
    order_id: str
    state: OrderState
    detail: str = ""


def emit_metric(name: str, value: float = 1, **dims: str) -> None:
    """CloudWatch Embedded Metric Format line (no extra IAM permissions needed)."""
    payload = {
        "_aws": {
            "Timestamp": 0,
            "CloudWatchMetrics": [
                {"Namespace": "EcomMcfBridge", "Dimensions": [list(dims)] if dims else [[]], "Metrics": [{"Name": name}]}
            ],
        },
        name: value,
        **dims,
    }
    import time

    payload["_aws"]["Timestamp"] = int(time.time() * 1000)
    print(json.dumps(payload))  # noqa: T201 - EMF is read from stdout


class McfBridge:
    def __init__(
        self,
        repo: Repository,
        client: SpApiClient,
        publisher: Publisher,
        settings: Settings,
        metric: Callable[..., None] = emit_metric,
    ):
        self.repo = repo
        self.client = client
        self.publisher = publisher
        self.settings = settings
        self.metric = metric

    # ------------------------------------------------------------------ order intake
    def build_request(self, order: PaidOrder) -> dict:
        marketplace_id = self.settings.marketplace_ids.get(order.marketplace)
        if not marketplace_id:
            raise ValidationError(f"unknown marketplace {order.marketplace!r}")
        items = []
        for idx, line in enumerate(order.lines, start=1):
            mapping = self.repo.get_mapping(line.storefront_sku, order.marketplace)
            if not mapping:
                raise ValidationError(f"no MSKU mapping for {line.storefront_sku} in {order.marketplace}")
            if mapping.get("status") == MappingStatus.INVALID.value:
                raise ValidationError(f"MSKU mapping for {line.storefront_sku} is INVALID")
            items.append(
                {
                    "sellerSku": mapping["msku"],
                    "sellerFulfillmentOrderItemId": f"{order.seller_fulfillment_order_id}-{idx}"[:50],
                    "quantity": line.quantity,
                }
            )
        body = {
            "marketplaceId": marketplace_id,
            "sellerFulfillmentOrderId": order.seller_fulfillment_order_id,
            "displayableOrderId": order.storefront_order_id[:40],
            "displayableOrderDate": order.placed_at,
            "displayableOrderComment": order.comment[:750],
            "shippingSpeedCategory": SHIPPING_SPEED[order.shipping_speed],
            "destinationAddress": order.address.to_spapi(),
            "fulfillmentAction": "Ship",
            "fulfillmentPolicy": order.fulfillment_policy or self.settings.default_fulfillment_policy,
            "items": items,
        }
        if self.settings.notification_emails:
            body["notificationEmails"] = list(self.settings.notification_emails)
        return body

    def submit(self, detail: dict, receive_count: int = 1) -> SubmitResult:
        """Handle one `Order Paid` event. Raises RetryLater for transient failures."""
        try:
            order = PaidOrder.from_event_detail(detail)
        except ValidationError as exc:
            order_id = str(detail.get("orderId", "unknown"))
            self._alert(order_id, f"invalid event: {exc}")
            return SubmitResult(order_id, OrderState.EXCEPTION, str(exc))

        rec = OrderRecord(
            storefront_order_id=order.storefront_order_id,
            state=OrderState.RECEIVED,
            seller_fulfillment_order_id=order.seller_fulfillment_order_id,
            marketplace=order.marketplace,
        )
        if not self.repo.create_order_if_absent(rec):
            existing = self.repo.get_order(order.storefront_order_id)
            if existing and existing.state != OrderState.RECEIVED:
                log.info("duplicate order event ignored: %s (%s)", existing.storefront_order_id, existing.state)
                return SubmitResult(existing.storefront_order_id, existing.state, "duplicate")
            rec = existing or rec

        try:
            body = self.build_request(order)
        except ValidationError as exc:
            return self._to_exception(rec, str(exc))

        rec.attempts = max(rec.attempts + 1, receive_count)
        try:
            self.client.create_fulfillment_order(body)
        except SpApiError as exc:
            if not exc.retryable:
                if self._already_at_mcf(rec):  # e.g. a retry after a lost acknowledgement
                    return self._mark_submitted(rec)
                return self._to_exception(rec, str(exc))
            rec.last_error = str(exc)
            if rec.attempts >= self.settings.max_attempts:
                self._to_exception(rec, f"gave up after {rec.attempts} attempts: {exc}")
                raise RetryLater(str(exc)) from exc  # let the queue move it to the DLQ as well
            self.repo.save_order(rec)
            self.metric("McfCreateRetry", 1)
            raise RetryLater(str(exc)) from exc

        return self._mark_submitted(rec)

    def _already_at_mcf(self, rec: OrderRecord) -> bool:
        try:
            self.client.get_fulfillment_order(rec.seller_fulfillment_order_id)
            return True
        except SpApiError:
            return False

    def _mark_submitted(self, rec: OrderRecord) -> SubmitResult:
        rec.state = OrderState.SUBMITTED
        rec.last_error = ""
        self.repo.save_order(rec)
        self.metric("McfOrderSubmitted", 1)
        self.publisher.publish(
            "Fulfillment Order Submitted",
            {"orderId": rec.storefront_order_id, "sellerFulfillmentOrderId": rec.seller_fulfillment_order_id},
        )
        return SubmitResult(rec.storefront_order_id, rec.state)

    def _to_exception(self, rec: OrderRecord, reason: str) -> SubmitResult:
        rec.state = OrderState.EXCEPTION
        rec.last_error = reason[:1000]
        self.repo.save_order(rec)
        self._alert(rec.storefront_order_id, reason)
        return SubmitResult(rec.storefront_order_id, rec.state, reason)

    def _alert(self, order_id: str, reason: str) -> None:
        log.error("order %s needs manual handling: %s", order_id, reason)
        self.metric("McfOrderException", 1)
        self.publisher.publish("Fulfillment Exception", {"orderId": order_id, "reason": reason[:500]})

    # ------------------------------------------------------------------ tracking
    def poll_tracking(self) -> dict[str, int]:
        counts = {"checked": 0, "shipped": 0, "completed": 0, "exceptions": 0}
        for state in (OrderState.SUBMITTED, OrderState.SHIPPED):
            for rec in self.repo.orders_in_state(state):
                counts["checked"] += 1
                try:
                    outcome = self._poll_one(rec)
                except SpApiError as exc:
                    log.warning("tracking poll failed for %s: %s", rec.storefront_order_id, exc)
                    continue
                if outcome:
                    counts[outcome] += 1
        return counts

    def _poll_one(self, rec: OrderRecord) -> str | None:
        payload = self.client.get_fulfillment_order(rec.seller_fulfillment_order_id).get("payload", {})
        status = payload.get("fulfillmentOrder", {}).get("fulfillmentOrderStatus")
        if status in {"Cancelled", "Unfulfillable", "Invalid"}:
            self._to_exception(rec, f"MCF order status {status}")
            return "exceptions"
        packages = []
        for shipment in payload.get("fulfillmentShipments", []) or []:
            if shipment.get("fulfillmentShipmentStatus") != "SHIPPED":
                continue
            for pkg in shipment.get("fulfillmentShipmentPackage", []) or []:
                details = self.client.get_package_tracking_details(int(pkg["packageNumber"])).get("payload", {})
                packages.append(
                    {
                        "packageNumber": pkg["packageNumber"],
                        "carrierCode": details.get("carrierCode") or pkg.get("carrierCode"),
                        "trackingNumber": details.get("trackingNumber") or pkg.get("trackingNumber"),
                        "trackingLink": details.get("customerTrackingLink", ""),
                        "currentStatus": details.get("currentStatus", "UNKNOWN"),
                    }
                )
        if not packages:
            return None
        previous = {p["packageNumber"]: p.get("currentStatus") for p in rec.packages}
        rec.packages = packages
        if all(p["currentStatus"] == "DELIVERED" for p in packages):
            rec.state = OrderState.COMPLETE
            self.repo.save_order(rec)
            self.publisher.publish("Fulfillment Delivered", {"orderId": rec.storefront_order_id, "packages": packages})
            return "completed"
        changed = rec.state != OrderState.SHIPPED or any(
            previous.get(p["packageNumber"]) != p["currentStatus"] for p in packages
        )
        rec.state = OrderState.SHIPPED
        self.repo.save_order(rec)
        if changed:
            self.publisher.publish("Fulfillment Shipped", {"orderId": rec.storefront_order_id, "packages": packages})
            return "shipped"
        return None

    # ------------------------------------------------------------------ inventory
    def sync_inventory(self) -> dict[str, int]:
        counts = {"skus": 0, "invalid": 0, "out_of_stock": 0}
        for marketplace, marketplace_id in self.settings.marketplace_ids.items():
            mappings = self.repo.mappings_for(marketplace)
            by_msku: dict[str, list[dict]] = {}
            for m in mappings:
                by_msku.setdefault(m["msku"], []).append(m)
            mskus = sorted(by_msku)
            found: dict[str, int] = {}
            for start in range(0, len(mskus), INVENTORY_BATCH):
                batch = mskus[start : start + INVENTORY_BATCH]
                token = None
                while True:
                    resp = self.client.get_inventory_summaries(marketplace_id, batch, token)
                    for s in resp.get("payload", {}).get("inventorySummaries", []):
                        qty = int((s.get("inventoryDetails") or {}).get("fulfillableQuantity", 0))
                        found[s["sellerSku"]] = qty
                    token = (resp.get("pagination") or {}).get("nextToken")
                    if not token:
                        break
            updates = []
            for msku, rows in by_msku.items():
                for m in rows:
                    counts["skus"] += 1
                    if msku not in found:
                        counts["invalid"] += 1
                        self.repo.update_mapping(m["storefrontSku"], marketplace, MappingStatus.INVALID, None)
                        updates.append({"sku": m["storefrontSku"], "msku": msku, "status": "INVALID", "qty": 0})
                        continue
                    qty = found[msku]
                    if qty == 0:
                        counts["out_of_stock"] += 1
                    self.repo.update_mapping(m["storefrontSku"], marketplace, MappingStatus.VALID, qty)
                    updates.append({"sku": m["storefrontSku"], "msku": msku, "status": "VALID", "qty": qty})
            for start in range(0, len(updates), 20):
                self.publisher.publish(
                    "Inventory Updated", {"marketplace": marketplace, "items": updates[start : start + 20]}
                )
        if counts["invalid"]:
            self.metric("McfInvalidSkuMapping", counts["invalid"])
        return counts


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES
