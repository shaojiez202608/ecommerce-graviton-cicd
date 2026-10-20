"""Outbox, redrive, confirmation, cancellation, multi-region and per-order isolation."""

from datetime import UTC, datetime, timedelta

import pytest
from mcf_bridge.models import OrderState
from mcf_bridge.service import (
    EV_CANCELLED,
    EV_DELIVERED,
    EV_SHIPPED,
    ListPublisher,
    McfBridge,
    RetryLater,
    Settings,
)
from mcf_bridge.spapi import InMemoryMockState, MockSpApiClient, SpApiError
from mcf_bridge.store import ConcurrentUpdate, InMemoryRepository


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 20, tzinfo=UTC)

    def __call__(self):
        return self.now


def order(order_id="1001", name="Alex Example", marketplace="US", **extra):
    d = {"orderId": order_id, "marketplace": marketplace, "placedAt": "2026-10-20T08:00:00Z",
         "shippingAddress": {"name": name, "addressLine1": "1 Example St", "postalCode": "98000", "countryCode": "US"},
         "lines": [{"sku": "SKU-A", "quantity": 1}]}
    d.update(extra)
    return d


@pytest.fixture
def env():
    clock = Clock()
    repo, pub = InMemoryRepository(), ListPublisher()
    client = MockSpApiClient(ship_after_seconds=60, clock=clock)
    settings = Settings(marketplace_ids={"US": "ATVPDKIKX0DER"}, stuck_received_minutes=30)
    bridge = McfBridge(repo, client, pub, settings, metric=lambda *a, **k: None, clock=clock)
    repo.put_mapping("SKU-A", "US", "MSKU-A")
    return bridge, repo, client, pub, clock


def types(pub):
    return [t for t, _ in pub.events]


# ---------------------------------------------------------------- A4: outbox
def test_publish_failure_keeps_state_and_event_then_poller_republishes(env):
    bridge, repo, _, pub, clock = env
    pub.fail = True
    assert bridge.submit(order()).state == OrderState.SUBMITTED
    rec = repo.get_order("1001")
    assert rec.state == OrderState.SUBMITTED and [e["type"] for e in rec.pending_events] == [
        "Fulfillment Order Submitted"]
    clock.now += timedelta(seconds=90)
    counts = bridge.poll_tracking()  # still failing: shipped event joins the outbox, nothing lost
    assert counts["shipped"] == 1 and counts["republished"] == 0
    assert [e["type"] for e in repo.get_order("1001").pending_events] == ["Fulfillment Order Submitted", EV_SHIPPED]
    pub.fail = False
    assert bridge.poll_tracking()["republished"] == 1
    assert types(pub) == ["Fulfillment Order Submitted", EV_SHIPPED]
    assert repo.get_order("1001").pending_events == []
    ids = [d["eventId"] for _, d in pub.events]
    assert ids == ["1001:1", "1001:2"]


def test_redelivery_flushes_outbox(env):
    bridge, repo, _, pub, _ = env
    pub.fail = True
    bridge.submit(order())
    pub.fail = False
    assert bridge.submit(order()).detail == "duplicate"
    assert types(pub) == ["Fulfillment Order Submitted"] and repo.get_order("1001").pending_events == []


def test_one_failing_order_does_not_stop_the_poll_round(env):
    bridge, repo, client, pub, clock = env
    repo.put_mapping("SKU-A", "US", "MSKU-A")
    bridge.submit(order("A"))
    bridge.submit(order("B"))
    clock.now += timedelta(seconds=90)
    real = client.get_fulfillment_order

    def broken(sfo):
        if sfo == "SF-A":
            raise RuntimeError("unexpected payload")
        return real(sfo)

    client.get_fulfillment_order = broken
    counts = bridge.poll_tracking()
    assert counts["errors"] == 1 and counts["shipped"] == 1
    assert repo.get_order("B").state == OrderState.SHIPPED and repo.get_order("A").state == OrderState.SUBMITTED


def test_optimistic_locking_rejects_stale_writes():
    repo = InMemoryRepository()
    from mcf_bridge.models import OrderRecord
    repo.create_order_if_absent(OrderRecord("X", OrderState.RECEIVED, "SF-X", "US"))
    a, b = repo.get_order("X"), repo.get_order("X")
    a.state = OrderState.SUBMITTED
    repo.save_order(a)
    b.state = OrderState.EXCEPTION
    with pytest.raises(ConcurrentUpdate):
        repo.save_order(b)


# ---------------------------------------------------------------- B1: redrive
def test_exception_order_recovers_with_retry_flag(env):
    bridge, repo, *_ = env
    for attempt in (1, 2, 3):
        with pytest.raises(RetryLater):
            bridge.submit(order(name="FAIL_FIRST_3"), receive_count=attempt)
    assert repo.get_order("1001").state == OrderState.EXCEPTION
    assert bridge.submit(order(name="FAIL_FIRST_3")).detail == "duplicate"  # plain replay is still ignored
    result = bridge.submit(order(name="FAIL_FIRST_3", retry=True), receive_count=1)
    assert result.state == OrderState.SUBMITTED
    rec = repo.get_order("1001")
    assert rec.attempts == 1 and rec.last_error == ""


# ---------------------------------------------------------------- B2: shipped before delivered
def test_direct_submitted_to_complete_still_emits_shipped(env):
    bridge, repo, _, pub, clock = env
    bridge.submit(order())
    clock.now += timedelta(seconds=200)  # first poll already sees DELIVERED
    assert bridge.poll_tracking()["completed"] == 1
    assert types(pub)[-2:] == [EV_SHIPPED, EV_DELIVERED]


# ---------------------------------------------------------------- B3: stuck RECEIVED -> EXCEPTION
def test_received_order_left_by_crashed_deliveries_is_swept(env):
    bridge, repo, client, pub, clock = env
    client.create_fulfillment_order = lambda body: (_ for _ in ()).throw(RuntimeError("Lambda timeout"))
    with pytest.raises(RuntimeError):
        bridge.submit(order())
    assert repo.get_order("1001").state == OrderState.RECEIVED
    assert bridge.poll_tracking()["stuck"] == 0
    clock.now += timedelta(minutes=31)
    # updated_at is wall-clock; make the record look old enough for the sweep
    rec = repo.get_order("1001")
    item = repo.items[("ORDER#1001", "META")]
    item["updatedAt"] = (clock.now - timedelta(minutes=31)).isoformat()
    assert rec.state == OrderState.RECEIVED
    assert bridge.poll_tracking()["stuck"] == 1
    assert repo.get_order("1001").state == OrderState.EXCEPTION and "DLQ" in repo.get_order("1001").last_error


# ---------------------------------------------------------------- B4: lost acknowledgement / unconfirmed
def test_lost_ack_is_recovered_on_the_next_attempt(env):
    bridge, repo, client, *_ = env
    with pytest.raises(RetryLater):
        bridge.submit(order(name="LOST_ACK"), receive_count=1)
    assert bridge.submit(order(name="LOST_ACK"), receive_count=2).state == OrderState.SUBMITTED
    assert client.calls.count("createFulfillmentOrder") == 2


def test_throttled_get_is_not_taken_as_missing(env):
    bridge, repo, client, *_ = env
    client.create_fulfillment_order = lambda body: (_ for _ in ()).throw(SpApiError(400, "duplicate"))
    client.get_fulfillment_order = lambda sfo: (_ for _ in ()).throw(SpApiError(429, "QuotaExceeded"))
    with pytest.raises(RetryLater):
        bridge.submit(order())
    assert repo.get_order("1001").state == OrderState.RECEIVED  # not marked EXCEPTION on a 429


def test_gave_up_unconfirmed_is_resolved_by_the_poller(env):
    bridge, repo, client, pub, clock = env
    real_create, real_get = client.create_fulfillment_order, client.get_fulfillment_order
    calls = {"n": 0}

    def create_then_timeout(body):
        calls["n"] += 1
        if calls["n"] == 3:
            real_create(body)  # the third attempt reached MCF but the response was lost
        raise SpApiError(504, "GatewayTimeout")

    client.create_fulfillment_order = create_then_timeout
    client.get_fulfillment_order = lambda sfo: (_ for _ in ()).throw(SpApiError(503, "busy"))
    for attempt in (1, 2, 3):
        with pytest.raises(RetryLater):
            bridge.submit(order(), receive_count=attempt)
    rec = repo.get_order("1001")
    assert rec.state == OrderState.EXCEPTION and rec.mcf_check and "unconfirmed" in rec.last_error
    client.get_fulfillment_order = real_get
    assert bridge.poll_tracking()["confirmed"] == 1
    assert repo.get_order("1001").state == OrderState.SUBMITTED
    clock.now += timedelta(seconds=90)
    assert bridge.poll_tracking()["shipped"] == 1


def test_gave_up_with_confirmation_that_mcf_has_nothing(env):
    bridge, repo, *_ = env
    for attempt in (1, 2, 3):
        with pytest.raises(RetryLater):
            bridge.submit(order(name="FAIL_ALWAYS"), receive_count=attempt)
    rec = repo.get_order("1001")
    assert rec.state == OrderState.EXCEPTION and not rec.mcf_check  # 404 from getFulfillmentOrder: definite


# ---------------------------------------------------------------- B5: marketplaces in several regions
def test_orders_and_inventory_use_the_marketplace_region():
    clock = Clock()
    state = InMemoryMockState()
    na, eu = (MockSpApiClient(state=state, ship_after_seconds=60, clock=clock, region=r) for r in ("na", "eu"))
    repo, pub = InMemoryRepository(), ListPublisher()
    settings = Settings(marketplace_ids={"US": "ATVPDKIKX0DER", "DE": "A1PA6795UKMFR9"},
                        marketplace_regions={"US": "na", "DE": "eu"})
    bridge = McfBridge(repo, {"na": na, "eu": eu}, pub, settings, metric=lambda *a, **k: None, clock=clock)
    repo.put_mapping("SKU-A", "US", "MSKU-A")
    repo.put_mapping("SKU-A", "DE", "MSKU-A-DE")
    bridge.submit(order("U1"))
    bridge.submit(order("D1", marketplace="DE"))
    assert na.calls.count("createFulfillmentOrder") == 1 and eu.calls.count("createFulfillmentOrder") == 1
    assert state.get("order#SF-D1")["region"] == "eu"
    bridge.sync_inventory()
    assert na.calls.count("getInventorySummaries") == 1 and eu.calls.count("getInventorySummaries") == 1
    clock.now += timedelta(seconds=90)
    bridge.poll_tracking()
    assert "getFulfillmentOrder" in eu.calls and repo.get_order("D1").state == OrderState.SHIPPED


# ---------------------------------------------------------------- B6: cancellation
def test_cancel_before_shipment_calls_mcf_and_marks_cancelled(env):
    bridge, repo, client, pub, clock = env
    bridge.submit(order())
    assert bridge.cancel({"orderId": "1001", "reason": "customer request"}).state == OrderState.CANCELLED
    assert "cancelFulfillmentOrder" in client.calls and types(pub)[-1] == EV_CANCELLED
    assert bridge.cancel({"orderId": "1001"}).detail == "duplicate"
    clock.now += timedelta(seconds=200)
    assert bridge.poll_tracking()["checked"] == 0  # cancelled orders are no longer tracked


def test_cancel_after_shipment_is_rejected_with_an_exception_event(env):
    bridge, repo, _, pub, clock = env
    bridge.submit(order())
    clock.now += timedelta(seconds=90)
    bridge.poll_tracking()
    result = bridge.cancel({"orderId": "1001"})
    assert result.state == OrderState.SHIPPED and "return" in result.detail
    assert types(pub)[-1] == "Fulfillment Exception"


def test_cancel_before_paid_event_blocks_the_late_paid_event(env):
    bridge, repo, client, *_ = env
    assert bridge.cancel({"orderId": "1001", "marketplace": "US"}).state == OrderState.CANCELLED
    assert bridge.submit(order()).detail == "duplicate"
    assert "createFulfillmentOrder" not in client.calls


def test_mcf_side_cancellation_becomes_cancelled_not_exception(env):
    bridge, repo, client, pub, _ = env
    bridge.submit(order())
    client.get_fulfillment_order = lambda sfo: {"payload": {"fulfillmentOrder": {"fulfillmentOrderStatus": "Cancelled"}}}
    assert bridge.poll_tracking()["cancelled"] == 1
    assert repo.get_order("1001").state == OrderState.CANCELLED
    assert pub.events[-1][1]["source"] == "mcf"


def test_cancel_of_exception_order_not_at_mcf_is_local(env):
    bridge, repo, client, *_ = env
    bridge.submit(order(name="INVALID_ADDRESS"))
    assert repo.get_order("1001").state == OrderState.EXCEPTION
    assert bridge.cancel({"orderId": "1001"}).state == OrderState.CANCELLED
    assert "cancelFulfillmentOrder" not in client.calls


def test_mock_fnsku_is_deterministic():
    a = MockSpApiClient().get_inventory_summaries("M", ["SKU-1"])["payload"]["inventorySummaries"][0]["fnSku"]
    b = MockSpApiClient().get_inventory_summaries("M", ["SKU-1"])["payload"]["inventorySummaries"][0]["fnSku"]
    assert a == b and a.startswith("X00") and len(a) == 10
