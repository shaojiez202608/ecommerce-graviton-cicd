"""Run the whole MCF bridge flow locally with the mock SP-API (no AWS needed).

    uv run python -m local.run_mcf_demo
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "mcf"))

from mcf_bridge.service import ListPublisher, McfBridge, RetryLater, Settings  # noqa: E402
from mcf_bridge.spapi import InMemoryMockState, MockSpApiClient  # noqa: E402
from mcf_bridge.store import InMemoryRepository  # noqa: E402


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def main() -> None:
    clock = Clock()
    repo, pub = InMemoryRepository(), ListPublisher()
    state = InMemoryMockState()
    clients = {r: MockSpApiClient(state=state, ship_after_seconds=60, clock=clock, region=r) for r in ("na", "eu")}
    settings = Settings(marketplace_ids={"US": "ATVPDKIKX0DER", "DE": "A1PA6795UKMFR9"},
                        marketplace_regions={"US": "na", "DE": "eu"})
    bridge = McfBridge(repo, clients, pub, settings, metric=lambda *a, **k: None, clock=clock)
    for m in json.loads((ROOT / "sample-data" / "sku_map.json").read_text())["mappings"]:
        repo.put_mapping(m["sku"], m["marketplace"], m["msku"])

    print("== inventory sync (validates SKU mappings, per marketplace / SP-API region)")
    print(bridge.sync_inventory())

    orders = json.loads((ROOT / "sample-data" / "orders.json").read_text())["orders"]
    print("== order intake (queue redelivers up to 3 times); EventBridge unavailable for the first order")
    for order in orders:
        pub.fail = order["orderId"] == "100000101"  # outbox: state advances, event waits
        for attempt in range(1, 4):
            try:
                r = bridge.submit(order, receive_count=attempt)
                print(f"  {r.order_id}: {r.state} {r.detail}")
                break
            except RetryLater as exc:
                print(f"  {order['orderId']}: attempt {attempt} -> retry later ({exc})")
    pub.fail = False
    print("  100000101 outbox:", [e["type"] for e in repo.get_order("100000101").pending_events])

    print("== storefront cancels 100000108 before it ships")
    print("  ", bridge.cancel({"orderId": "100000108", "reason": "customer request"}))

    print("== operator redrives 100000107 from the DLQ (retry=true)")
    redriven = next(o for o in orders if o["orderId"] == "100000107")
    print("  ", bridge.submit(dict(redriven, retry=True)))

    for minutes in (1.5, 2):
        clock.now += timedelta(minutes=minutes)
        print(f"== tracking poll (+{minutes} min)", bridge.poll_tracking())

    print("== final order states")
    for o in orders:
        rec = repo.get_order(o["orderId"])
        region = (state.get(f"order#{rec.seller_fulfillment_order_id}") or {}).get("region", "-")
        print(f"  {o['orderId']}: {rec.state} region={region} "
              f"packages={rec.packages and rec.packages[0]['trackingNumber']} err={rec.last_error[:60]}")
    print("== events published to the storefront bus")
    for detail_type, detail in pub.events:
        print(f"  {detail_type}: {json.dumps(detail)[:120]}")


if __name__ == "__main__":
    main()
