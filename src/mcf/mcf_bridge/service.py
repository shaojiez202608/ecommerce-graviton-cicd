"""Business logic: order-paid -> createFulfillmentOrder -> tracking -> storefront events.

Pure Python; AWS access is injected (repository, SP-API clients, event publisher).

Delivery guarantees
- Every state change and the events it causes are written in one conditional item write
  (transactional outbox, ``OrderRecord.pending_events``). Events are published afterwards and
  removed from the outbox only once EventBridge accepted them. A failed publish leaves the state
  change in place and the event pending; the tracking poller and any redelivery publish it again.
  Delivery is at-least-once; every event carries a stable ``eventId`` for de-duplication.
- The tracking poller handles each order independently: one failing order never stops the round.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import (
    SHIPPING_SPEED,
    TERMINAL_STATES,
    MappingStatus,
    OrderRecord,
    OrderState,
    PaidOrder,
    ValidationError,
    seller_fulfillment_order_id,
)
from .spapi import SpApiClient, SpApiError
from .store import ConcurrentUpdate, Repository

log = logging.getLogger(__name__)

EVENT_SOURCE = "mcf.bridge"
INVENTORY_BATCH = 50  # getInventorySummaries sellerSkus maxItems in the official model

EV_SUBMITTED = "Fulfillment Order Submitted"
EV_SHIPPED = "Fulfillment Shipped"
EV_DELIVERED = "Fulfillment Delivered"
EV_CANCELLED = "Fulfillment Cancelled"
EV_EXCEPTION = "Fulfillment Exception"
EV_INVENTORY = "Inventory Updated"


class Publisher(Protocol):
    def publish(self, detail_type: str, detail: dict) -> None: ...


class ListPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.fail = False  # tests: simulate EventBridge being unavailable

    def publish(self, detail_type, detail):
        if self.fail:
            raise RuntimeError("PutEvents failed (simulated)")
        self.events.append((detail_type, detail))


@dataclass
class Settings:
    marketplace_ids: dict[str, str]  # logical key -> SP-API marketplaceId
    marketplace_regions: dict[str, str] = field(default_factory=dict)  # logical key -> na | eu | fe
    default_fulfillment_policy: str = "FillOrKill"
    max_attempts: int = 3
    notification_emails: list[str] = field(default_factory=list)
    # RECEIVED for longer than this (minutes) means every redelivery failed and the message is in
    # the DLQ; the poller then marks the order EXCEPTION. 0 disables the sweep.
    stuck_received_minutes: int = 0


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
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {"Namespace": "EcomMcfBridge", "Dimensions": [list(dims)] if dims else [[]], "Metrics": [{"Name": name}]}
            ],
        },
        name: value,
        **dims,
    }
    print(json.dumps(payload))  # noqa: T201 - EMF is read from stdout


def _now() -> datetime:
    return datetime.now(UTC)


class McfBridge:
    def __init__(
        self,
        repo: Repository,
        client: SpApiClient | Mapping[str, SpApiClient],
        publisher: Publisher,
        settings: Settings,
        metric: Callable[..., None] = emit_metric,
        clock: Callable[[], datetime] = _now,
    ):
        self.repo = repo
        # One SP-API client per endpoint region (na/eu/fe); a single client serves every region.
        self.clients = dict(client) if isinstance(client, Mapping) else {"*": client}
        self.publisher = publisher
        self.settings = settings
        self.metric = metric
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def client_for(self, marketplace: str) -> SpApiClient:
        if "*" in self.clients:
            return self.clients["*"]
        region = self.settings.marketplace_regions.get(marketplace)
        if region not in self.clients:
            raise ValidationError(f"no SP-API client for marketplace {marketplace!r} (region {region!r})")
        return self.clients[region]

    @property
    def client(self) -> SpApiClient:
        """The single client (tests/local demo with one region)."""
        return next(iter(self.clients.values()))

    def _emit(self, rec: OrderRecord, detail_type: str, detail: dict) -> None:
        rec.event_seq += 1
        rec.pending_events.append({
            "type": detail_type,
            "detail": {"eventId": f"{rec.storefront_order_id}:{rec.event_seq}", "orderId": rec.storefront_order_id,
                       **detail},
        })

    def _commit(self, rec: OrderRecord) -> None:
        """Persist state + outbox in one conditional write, then try to deliver the outbox."""
        self.repo.save_order(rec)
        self._flush(rec)

    def _flush(self, rec: OrderRecord) -> bool:
        """Publish pending events in order; keep the rest if one fails. True when the outbox is empty."""
        sent = 0
        for event in rec.pending_events:
            try:
                self.publisher.publish(event["type"], event["detail"])
            except Exception as exc:  # noqa: BLE001 - any publish failure leaves the event pending
                log.warning("publish %s for %s failed, kept in outbox: %s", event["type"], rec.storefront_order_id, exc)
                self.metric("McfEventPublishFailed", 1)
                break
            sent += 1
        if sent:
            rec.pending_events = rec.pending_events[sent:]
            try:
                self.repo.save_order(rec)
            except ConcurrentUpdate:
                # A newer version still lists these events; they will be sent again (same eventId).
                log.info("outbox of %s changed concurrently; delivered events may repeat", rec.storefront_order_id)
                return False
        return not rec.pending_events

    def _mcf_has_order(self, rec: OrderRecord) -> bool:
        """Definite answer from getFulfillmentOrder, or RetryLater when MCF cannot tell us now."""
        try:
            self.client_for(rec.marketplace).get_fulfillment_order(rec.seller_fulfillment_order_id)
            return True
        except SpApiError as exc:
            if exc.status in (400, 404):
                return False
            if exc.retryable:
                raise RetryLater(f"cannot confirm MCF state: {exc}") from exc
            raise

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
        """Handle one `Order Paid` event. Raises RetryLater for transient failures.

        ``detail.retry = true`` (set by scripts/mcf_redrive.py or a re-published storefront event)
        resets an EXCEPTION order to RECEIVED with a fresh attempt budget.
        """
        try:
            order = PaidOrder.from_event_detail(detail)
        except ValidationError as exc:
            order_id = str(detail.get("orderId", "unknown"))
            log.error("order %s needs manual handling: invalid event: %s", order_id, exc)
            self.metric("McfOrderException", 1)
            # No order record to attach an outbox to; a publish failure raises and the queue retries.
            self.publisher.publish(EV_EXCEPTION, {"orderId": order_id, "reason": f"invalid event: {exc}"[:500]})
            return SubmitResult(order_id, OrderState.EXCEPTION, str(exc))

        rec = OrderRecord(
            storefront_order_id=order.storefront_order_id,
            state=OrderState.RECEIVED,
            seller_fulfillment_order_id=order.seller_fulfillment_order_id,
            marketplace=order.marketplace,
        )
        if not self.repo.create_order_if_absent(rec):
            rec = self.repo.get_order(order.storefront_order_id)
            if rec.pending_events:
                self._flush(rec)
                rec = self.repo.get_order(order.storefront_order_id)
            if rec.state == OrderState.EXCEPTION and detail.get("retry") is True:
                log.info("retry requested for %s (previous error: %s)", rec.storefront_order_id, rec.last_error)
                rec.state, rec.attempts, rec.mcf_check = OrderState.RECEIVED, 0, False
                rec.last_error = f"retry requested; previous error: {rec.last_error}"[:1000]
                receive_count = 1
                self._save_or_retry(rec)
            elif rec.state != OrderState.RECEIVED:
                log.info("duplicate order event ignored: %s (%s)", rec.storefront_order_id, rec.state)
                return SubmitResult(rec.storefront_order_id, rec.state, "duplicate")

        try:
            body = self.build_request(order)
            client = self.client_for(order.marketplace)
        except ValidationError as exc:
            return self._to_exception(rec, str(exc))

        rec.attempts = max(rec.attempts + 1, receive_count)
        try:
            client.create_fulfillment_order(body)
        except SpApiError as exc:
            if not exc.retryable:
                # e.g. a retry after a lost acknowledgement: MCF rejects the duplicate id
                if self._mcf_has_order(rec):
                    return self._mark_submitted(rec)
                return self._to_exception(rec, str(exc))
            rec.last_error = str(exc)[:1000]
            if rec.attempts >= self.settings.max_attempts:
                try:
                    known = self._mcf_has_order(rec)  # the last timeout may still have created it
                except RetryLater:
                    known = None
                if known:
                    return self._mark_submitted(rec)
                rec.mcf_check = known is None
                note = "; MCF state unconfirmed, poller keeps checking" if rec.mcf_check else ""
                self._to_exception(rec, f"gave up after {rec.attempts} attempts: {exc}{note}")
                raise RetryLater(str(exc)) from exc  # let the queue move it to the DLQ as well
            self._save_or_retry(rec)
            self.metric("McfCreateRetry", 1)
            raise RetryLater(str(exc)) from exc

        return self._mark_submitted(rec)

    def _save_or_retry(self, rec: OrderRecord) -> None:
        try:
            self.repo.save_order(rec)
        except ConcurrentUpdate as exc:
            raise RetryLater(f"order {rec.storefront_order_id} changed concurrently") from exc

    def _mark_submitted(self, rec: OrderRecord) -> SubmitResult:
        rec.state, rec.last_error, rec.mcf_check = OrderState.SUBMITTED, "", False
        self._emit(rec, EV_SUBMITTED, {"sellerFulfillmentOrderId": rec.seller_fulfillment_order_id})
        try:
            self._commit(rec)
        except ConcurrentUpdate:
            latest = self.repo.get_order(rec.storefront_order_id)
            if latest and latest.state == OrderState.CANCELLED:
                # Cancelled by the storefront while createFulfillmentOrder was in flight.
                self._cancel_at_mcf_best_effort(latest)
                return SubmitResult(latest.storefront_order_id, latest.state, "cancelled during submit")
            raise RetryLater(f"order {rec.storefront_order_id} changed concurrently") from None
        self.metric("McfOrderSubmitted", 1)
        return SubmitResult(rec.storefront_order_id, rec.state)

    def _to_exception(self, rec: OrderRecord, reason: str) -> SubmitResult:
        rec.state = OrderState.EXCEPTION
        rec.last_error = reason[:1000]
        log.error("order %s needs manual handling: %s", rec.storefront_order_id, reason)
        self.metric("McfOrderException", 1)
        self._emit(rec, EV_EXCEPTION, {"reason": reason[:500]})
        self._commit(rec)
        return SubmitResult(rec.storefront_order_id, rec.state, reason)

    # ------------------------------------------------------------------ cancellation
    def cancel(self, detail: dict) -> SubmitResult:
        """Handle one `Order Cancelled` event (storefront-side cancellation before shipment)."""
        order_id = str(detail.get("orderId") or "")
        if not order_id:
            raise ValidationError("cancel event without orderId")
        reason = str(detail.get("reason") or "cancelled by storefront")[:500]
        rec = self.repo.get_order(order_id)
        if rec is None:
            # Cancelled before the paid event arrived: record it so a late `Order Paid` is ignored.
            rec = OrderRecord(order_id, OrderState.CANCELLED, seller_fulfillment_order_id(order_id),
                              str(detail.get("marketplace", "")))
            if self.repo.create_order_if_absent(rec):
                return SubmitResult(order_id, OrderState.CANCELLED, "cancelled before submission")
            rec = self.repo.get_order(order_id)
        if rec.state == OrderState.CANCELLED:
            return SubmitResult(order_id, rec.state, "duplicate")
        if rec.state in (OrderState.SHIPPED, OrderState.COMPLETE):
            return self._cancel_rejected(rec, "cancel requested after shipment; handle it as a return")
        try:
            at_mcf = rec.state == OrderState.SUBMITTED or self._mcf_has_order(rec)
            if at_mcf:
                self.client_for(rec.marketplace).cancel_fulfillment_order(rec.seller_fulfillment_order_id)
        except SpApiError as exc:
            if exc.retryable:
                raise RetryLater(str(exc)) from exc
            return self._cancel_rejected(rec, f"cancelFulfillmentOrder rejected: {exc}")
        rec.state, rec.mcf_check = OrderState.CANCELLED, False
        self._emit(rec, EV_CANCELLED, {"source": "storefront", "reason": reason,
                                       "sellerFulfillmentOrderId": rec.seller_fulfillment_order_id})
        try:
            self._commit(rec)
        except ConcurrentUpdate as exc:
            raise RetryLater(f"order {order_id} changed concurrently") from exc
        self.metric("McfOrderCancelled", 1)
        return SubmitResult(order_id, rec.state)

    def _cancel_rejected(self, rec: OrderRecord, reason: str) -> SubmitResult:
        log.error("order %s: %s", rec.storefront_order_id, reason)
        self.metric("McfOrderException", 1)
        self._emit(rec, EV_EXCEPTION, {"reason": reason[:500], "state": rec.state.value})
        try:
            self._commit(rec)
        except ConcurrentUpdate as exc:
            raise RetryLater(f"order {rec.storefront_order_id} changed concurrently") from exc
        return SubmitResult(rec.storefront_order_id, rec.state, reason)

    def _cancel_at_mcf_best_effort(self, rec: OrderRecord) -> None:
        try:
            self.client_for(rec.marketplace).cancel_fulfillment_order(rec.seller_fulfillment_order_id)
        except Exception as exc:  # noqa: BLE001 - surfaced as an exception event for a human
            self._cancel_rejected(rec, f"order was cancelled but MCF cancel failed: {exc}")

    # ------------------------------------------------------------------ tracking
    def poll_tracking(self) -> dict[str, int]:
        counts = {"checked": 0, "shipped": 0, "completed": 0, "cancelled": 0, "exceptions": 0,
                  "republished": 0, "confirmed": 0, "stuck": 0, "errors": 0}

        def each(records, fn):
            for stale in records:
                try:
                    rec = self.repo.get_order(stale.storefront_order_id)  # consistent read before acting
                    if rec is None:
                        continue
                    outcome = fn(rec)
                except ConcurrentUpdate:
                    log.info("order %s changed concurrently; next round", stale.storefront_order_id)
                    continue
                except Exception:  # noqa: BLE001 - one broken order must not stop the round
                    counts["errors"] += 1
                    log.exception("tracking step failed for %s", stale.storefront_order_id)
                    continue
                if outcome:
                    counts[outcome] += 1

        # 1. outbox: events whose publish failed earlier
        each(self.repo.orders_with_pending_events(), lambda r: "republished" if r.pending_events and self._flush(r)
             else None)
        # 2. orders given up without a definite answer from MCF
        each(self.repo.orders_in_state(OrderState.EXCEPTION), self._confirm_at_mcf)
        # 3. orders whose every delivery failed before reaching a decision (message in the DLQ)
        if self.settings.stuck_received_minutes:
            each(self.repo.orders_in_state(OrderState.RECEIVED), self._sweep_stuck)
        # 4. shipment tracking
        records = [*self.repo.orders_in_state(OrderState.SUBMITTED), *self.repo.orders_in_state(OrderState.SHIPPED)]
        counts["checked"] = len(records)
        each(records, self._poll_one)
        if counts["errors"]:
            self.metric("McfTrackingErrors", counts["errors"])
        return counts

    def _confirm_at_mcf(self, rec: OrderRecord) -> str | None:
        if rec.state != OrderState.EXCEPTION or not rec.mcf_check:
            return None
        try:
            found = self._mcf_has_order(rec)
        except RetryLater:
            return None  # still no answer; ask again next round
        if found:
            log.info("order %s exists at MCF after all; resuming tracking", rec.storefront_order_id)
            self._mark_submitted(rec)
            return "confirmed"
        rec.mcf_check = False
        rec.last_error = f"{rec.last_error}; confirmed: MCF has no such order"[:1000]
        self.repo.save_order(rec)
        return None

    def _sweep_stuck(self, rec: OrderRecord) -> str | None:
        if rec.state != OrderState.RECEIVED or not rec.updated_at:
            return None
        age = self.clock() - datetime.fromisoformat(rec.updated_at)
        if age < timedelta(minutes=self.settings.stuck_received_minutes):
            return None
        self._to_exception(rec, f"no outcome after all deliveries (last error: {rec.last_error or 'none'}); "
                                "the message is in the DLQ, redrive with scripts/mcf_redrive.py")
        return "stuck"

    def _poll_one(self, rec: OrderRecord) -> str | None:
        if rec.state not in (OrderState.SUBMITTED, OrderState.SHIPPED):
            return None
        client = self.client_for(rec.marketplace)
        payload = client.get_fulfillment_order(rec.seller_fulfillment_order_id).get("payload", {})
        status = payload.get("fulfillmentOrder", {}).get("fulfillmentOrderStatus")
        if status == "Cancelled":
            rec.state = OrderState.CANCELLED
            self._emit(rec, EV_CANCELLED, {"source": "mcf", "sellerFulfillmentOrderId": rec.seller_fulfillment_order_id})
            self._commit(rec)
            return "cancelled"
        if status in {"Unfulfillable", "Invalid"}:
            self._to_exception(rec, f"MCF order status {status}")
            return "exceptions"
        packages = []
        for shipment in payload.get("fulfillmentShipments", []) or []:
            if shipment.get("fulfillmentShipmentStatus") != "SHIPPED":
                continue
            for pkg in shipment.get("fulfillmentShipmentPackage", []) or []:
                details = client.get_package_tracking_details(int(pkg["packageNumber"])).get("payload", {})
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
        first_shipment = rec.state != OrderState.SHIPPED
        rec.packages = packages
        if all(p["currentStatus"] == "DELIVERED" for p in packages):
            if first_shipment:  # shipped and delivered between two polls: still tell the storefront it shipped
                self._emit(rec, EV_SHIPPED, {"packages": packages})
            rec.state = OrderState.COMPLETE
            self._emit(rec, EV_DELIVERED, {"packages": packages})
            self._commit(rec)
            return "completed"
        changed = first_shipment or any(previous.get(p["packageNumber"]) != p["currentStatus"] for p in packages)
        rec.state = OrderState.SHIPPED
        if changed:
            self._emit(rec, EV_SHIPPED, {"packages": packages})
            self._commit(rec)
            return "shipped"
        return None

    # ------------------------------------------------------------------ inventory
    def sync_inventory(self) -> dict[str, int]:
        """Mapping updates are idempotent and events carry absolute quantities, so a failed publish
        simply fails the run (Lambda error alarm) and the next scheduled run sends fresh values."""
        counts = {"skus": 0, "invalid": 0, "out_of_stock": 0}
        for marketplace, marketplace_id in self.settings.marketplace_ids.items():
            client = self.client_for(marketplace)
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
                    resp = client.get_inventory_summaries(marketplace_id, batch, token)
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
                    EV_INVENTORY, {"marketplace": marketplace, "items": updates[start : start + 20]}
                )
        if counts["invalid"]:
            self.metric("McfInvalidSkuMapping", counts["invalid"])
        return counts


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES
