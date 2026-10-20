from datetime import UTC, datetime, timedelta

import pytest
from mcf_bridge.models import (
    MappingStatus,
    OrderState,
    PaidOrder,
    ValidationError,
    seller_fulfillment_order_id,
)
from mcf_bridge.service import ListPublisher, McfBridge, RetryLater, Settings
from mcf_bridge.spapi import MockSpApiClient, SpApiError
from mcf_bridge.store import InMemoryRepository


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 20, tzinfo=UTC)

    def __call__(self):
        return self.now


def order(order_id="1001", name="Alex Example", lines=None, **extra):
    d = {
        "orderId": order_id,
        "marketplace": "US",
        "placedAt": "2026-10-20T08:00:00Z",
        "shippingSpeed": "standard",
        "shippingAddress": {"name": name, "addressLine1": "1 Example St", "city": "Springfield",
                            "stateOrRegion": "WA", "postalCode": "98000", "countryCode": "US"},
        "lines": lines if lines is not None else [{"sku": "SKU-A", "quantity": 2}],
    }
    d.update(extra)
    return d


@pytest.fixture
def env():
    clock = Clock()
    repo, pub, metrics = InMemoryRepository(), ListPublisher(), []
    client = MockSpApiClient(ship_after_seconds=60, clock=clock)
    bridge = McfBridge(repo, client, pub, Settings(marketplace_ids={"US": "ATVPDKIKX0DER"}),
                       metric=lambda name, value=1, **_: metrics.append(name))
    repo.put_mapping("SKU-A", "US", "MSKU-A")
    repo.put_mapping("SKU-B", "US", "MSKU-B")
    return bridge, repo, client, pub, clock, metrics


def test_request_matches_official_model_fields(env):
    bridge, *_ = env
    body = bridge.build_request(PaidOrder.from_event_detail(order(shippingSpeed="expedited")))
    for required in ("sellerFulfillmentOrderId", "displayableOrderId", "displayableOrderDate",
                     "displayableOrderComment", "shippingSpeedCategory", "destinationAddress", "items"):
        assert required in body
    assert body["shippingSpeedCategory"] == "Expedited"
    assert body["fulfillmentPolicy"] == "FillOrKill"
    assert body["marketplaceId"] == "ATVPDKIKX0DER"
    assert body["items"][0] == {"sellerSku": "MSKU-A", "sellerFulfillmentOrderItemId": "SF-1001-1", "quantity": 2}
    assert {"name", "addressLine1", "postalCode", "countryCode"} <= set(body["destinationAddress"])


def test_seller_fulfillment_order_id_is_deterministic_and_bounded():
    long_id = "X" * 100
    a, b = seller_fulfillment_order_id(long_id), seller_fulfillment_order_id(long_id)
    assert a == b and len(a) <= 40
    assert seller_fulfillment_order_id("12/34") == "SF-12-34"


def test_happy_path_submit_ship_deliver(env):
    bridge, repo, client, pub, clock, _ = env
    assert bridge.submit(order()).state == OrderState.SUBMITTED
    clock.now += timedelta(seconds=90)
    assert bridge.poll_tracking()["shipped"] == 1
    rec = repo.get_order("1001")
    assert rec.state == OrderState.SHIPPED and rec.packages[0]["trackingNumber"].startswith("MOCKTRK")
    assert bridge.poll_tracking()["shipped"] == 0  # unchanged status -> no duplicate event
    clock.now += timedelta(seconds=60)
    assert bridge.poll_tracking()["completed"] == 1
    assert repo.get_order("1001").state == OrderState.COMPLETE
    types = [t for t, _ in pub.events]
    assert types == ["Fulfillment Order Submitted", "Fulfillment Shipped", "Fulfillment Delivered"]


def test_duplicate_event_is_idempotent(env):
    bridge, _, client, *_ = env
    bridge.submit(order())
    result = bridge.submit(order())
    assert result.detail == "duplicate"
    assert client.calls.count("createFulfillmentOrder") == 1


def test_transient_errors_retry_then_succeed(env):
    bridge, repo, *_ = env
    with pytest.raises(RetryLater):
        bridge.submit(order(name="FAIL_TRANSIENT"), receive_count=1)
    with pytest.raises(RetryLater):
        bridge.submit(order(name="FAIL_TRANSIENT"), receive_count=2)
    assert repo.get_order("1001").state == OrderState.RECEIVED
    assert bridge.submit(order(name="FAIL_TRANSIENT"), receive_count=3).state == OrderState.SUBMITTED


def test_gives_up_after_max_attempts_and_alerts(env):
    bridge, repo, _, pub, _, metrics = env
    for attempt in (1, 2, 3):
        with pytest.raises(RetryLater):
            bridge.submit(order(name="FAIL_ALWAYS"), receive_count=attempt)
    rec = repo.get_order("1001")
    assert rec.state == OrderState.EXCEPTION and "gave up after 3" in rec.last_error
    assert "McfOrderException" in metrics
    assert pub.events[-1][0] == "Fulfillment Exception"
    # a late redelivery must not resubmit
    assert bridge.submit(order(name="FAIL_ALWAYS"), receive_count=4).detail == "duplicate"


def test_non_retryable_error_marks_exception_without_retry(env):
    bridge, repo, *_ = env
    assert bridge.submit(order(name="INVALID_ADDRESS")).state == OrderState.EXCEPTION
    assert "400" in repo.get_order("1001").last_error


def test_lost_ack_is_recovered_via_get(env):
    bridge, repo, client, *_ = env
    body = bridge.build_request(PaidOrder.from_event_detail(order()))
    client.create_fulfillment_order(body)  # MCF has the order, but our state never recorded it

    original = client.create_fulfillment_order

    def reject_duplicate(b):
        raise SpApiError(400, "duplicate sellerFulfillmentOrderId")

    client.create_fulfillment_order = reject_duplicate
    assert bridge.submit(order()).state == OrderState.SUBMITTED
    client.create_fulfillment_order = original


def test_unmapped_and_invalid_skus(env):
    bridge, repo, *_ = env
    r = bridge.submit(order(lines=[{"sku": "NOPE", "quantity": 1}]))
    assert r.state == OrderState.EXCEPTION and "no MSKU mapping" in r.detail
    repo.update_mapping("SKU-B", "US", MappingStatus.INVALID, None)
    r = bridge.submit(order(order_id="1002", lines=[{"sku": "SKU-B", "quantity": 1}]))
    assert r.state == OrderState.EXCEPTION and "INVALID" in r.detail


@pytest.mark.parametrize("bad", [
    {"lines": []},
    {"lines": [{"sku": "SKU-A", "quantity": 0}]},
    {"shippingSpeed": "teleport"},
    {"fulfillmentPolicy": "Whatever"},
])
def test_malformed_events_are_rejected(env, bad):
    bridge, *_ = env
    assert bridge.submit(order(**bad)).state == OrderState.EXCEPTION


def test_address_missing_required_field():
    d = order()
    del d["shippingAddress"]["postalCode"]
    with pytest.raises(ValidationError):
        PaidOrder.from_event_detail(d)


def test_cancelled_mcf_order_becomes_exception(env):
    bridge, repo, client, *_ = env
    bridge.submit(order())
    client.get_fulfillment_order = lambda sfo: {"payload": {"fulfillmentOrder": {"fulfillmentOrderStatus": "Unfulfillable"}}}
    assert bridge.poll_tracking()["exceptions"] == 1
    assert repo.get_order("1001").state == OrderState.EXCEPTION


def test_inventory_sync_marks_invalid_and_out_of_stock(env):
    bridge, repo, _, pub, _, metrics = env
    repo.put_mapping("SKU-C", "US", "MSKU-C-OOS")
    repo.put_mapping("SKU-D", "US", "MSKU-D-MISSING")
    counts = bridge.sync_inventory()
    assert counts == {"skus": 4, "invalid": 1, "out_of_stock": 1}
    assert repo.get_mapping("SKU-D", "US")["status"] == "INVALID"
    assert repo.get_mapping("SKU-C", "US")["fulfillableQuantity"] == 0
    assert repo.get_mapping("SKU-A", "US")["status"] == "VALID"
    assert "McfInvalidSkuMapping" in metrics
    assert pub.events[0][0] == "Inventory Updated"


def test_inventory_sync_batches_and_paginates(env):
    bridge, repo, client, *_ = env
    for i in range(120):
        repo.put_mapping(f"BULK-{i}", "US", f"MSKU-BULK-{i}")
    calls = []
    real = client.get_inventory_summaries

    def paged(mid, skus, token=None):
        calls.append((len(skus), token))
        assert len(skus) <= 50
        resp = real(mid, skus, token)
        if token is None:
            half = resp["payload"]["inventorySummaries"]
            resp["payload"]["inventorySummaries"] = half[: len(half) // 2]
            resp["pagination"] = {"nextToken": "page2"}
            paged.rest = half[len(half) // 2 :]
        else:
            resp["payload"]["inventorySummaries"] = paged.rest
        return resp

    client.get_inventory_summaries = paged
    counts = bridge.sync_inventory()
    assert counts["invalid"] == 0 and counts["skus"] == 122
    assert [c[1] for c in calls].count("page2") == 3  # 3 batches, each with a second page
