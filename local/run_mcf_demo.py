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
from mcf_bridge.spapi import MockSpApiClient  # noqa: E402
from mcf_bridge.store import InMemoryRepository  # noqa: E402


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def main() -> None:
    clock = Clock()
    repo, pub = InMemoryRepository(), ListPublisher()
    client = MockSpApiClient(ship_after_seconds=60, clock=clock)
    bridge = McfBridge(repo, client, pub, Settings(marketplace_ids={"US": "ATVPDKIKX0DER"}),
                       metric=lambda *a, **k: None)
    for m in json.loads((ROOT / "sample-data" / "sku_map.json").read_text())["mappings"]:
        repo.put_mapping(m["sku"], m["marketplace"], m["msku"])

    print("== inventory sync (validates SKU mappings)")
    print(bridge.sync_inventory())

    print("== order intake (queue redelivers up to 3 times)")
    for order in json.loads((ROOT / "sample-data" / "orders.json").read_text())["orders"]:
        for attempt in range(1, 4):
            try:
                r = bridge.submit(order, receive_count=attempt)
                print(f"  {r.order_id}: {r.state} {r.detail}")
                break
            except RetryLater as exc:
                print(f"  {order['orderId']}: attempt {attempt} -> retry later ({exc})")

    for minutes in (1.5, 2):
        clock.now += timedelta(minutes=minutes)
        print(f"== tracking poll (+{minutes} min)", bridge.poll_tracking())

    print("== final order states")
    for oid in ("100000101", "100000102", "100000103", "100000104"):
        rec = repo.get_order(oid)
        print(f"  {oid}: {rec.state} packages={rec.packages and rec.packages[0]['trackingNumber']} err={rec.last_error[:60]}")
    print("== events published to the storefront bus")
    for detail_type, detail in pub.events:
        print(f"  {detail_type}: {json.dumps(detail)[:120]}")


if __name__ == "__main__":
    main()
