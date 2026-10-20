"""Deployed MCF bridge end-to-end check (mock SP-API).

Steps
 1. seed the SKU map, run inventory sync (both marketplaces / SP-API regions)
 2. outbox drill (--outbox-drill): deny events:PutEvents on the bridge roles, send one order,
    check that its state advanced with the event kept in the outbox, lift the deny, run the
    tracking poller and check that the event was republished
 3. publish the synthetic `Order Paid` events, cancel one order before it ships
 4. run tracking polls until every order settled; redrive the DLQ once with retry=true
 5. print final states, the per-order event sequence seen by the stand-in consumer (log group),
    the SP-API region recorded by the mock for each order, and the DLQ depth

    uv run python scripts/mcf_e2e.py --env dev [--config config/sandbox.json] [--outbox-drill]
Only touches resources of the named stack (the drill adds and removes one inline policy on the
stack's own Lambda roles); prints no account identifiers.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]


def profile_project(path: Path) -> str:
    """Project prefix from the profile (local copy, or the committed template when there is none)."""
    if not path.exists():
        path = path.with_name(path.stem + ".example.json")
    return json.loads(path.read_text())["project"]
sys.path.insert(0, str(ROOT / "src" / "mcf"))
sys.path.insert(0, str(ROOT / "scripts"))
from mcf_bridge.store import DynamoRepository  # noqa: E402
from mcf_redrive import redrive  # noqa: E402

DENY_POLICY = "e2e-outbox-drill-deny-putevents"
SETTLED = {"COMPLETE", "EXCEPTION", "CANCELLED"}


def outputs(cfn, stack):
    return {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]}


class Env:
    def __init__(self, prefix: str, env: str):
        self.p = f"{prefix}-{env}"
        out = outputs(boto3.client("cloudformation"), f"{self.p}-mcf")
        self.table = boto3.resource("dynamodb").Table(out["StateTableName"])
        self.repo = DynamoRepository(self.table)
        self.bus = out["EventBusName"]
        self.log_group = out["BridgeEventsLogGroup"]
        self.lam = boto3.client("lambda")
        self.events = boto3.client("events")
        self.iam = boto3.client("iam")
        self.logs = boto3.client("logs")
        self.sqs = boto3.client("sqs")

    def invoke(self, name: str) -> dict:
        return json.loads(self.lam.invoke(FunctionName=f"{self.p}-mcf-{name}")["Payload"].read())

    def put(self, detail_type: str, detail: dict) -> None:
        resp = self.events.put_events(Entries=[{"Source": "storefront.orders", "DetailType": detail_type,
                                                "Detail": json.dumps(detail), "EventBusName": self.bus}])
        assert resp["FailedEntryCount"] == 0

    def state(self, order_id: str) -> str:
        rec = self.repo.get_order(order_id)
        return rec.state.value if rec else "-"

    def roles(self) -> list[str]:
        names = []
        for fn in ("intake", "tracking"):
            arn = self.lam.get_function_configuration(FunctionName=f"{self.p}-mcf-{fn}")["Role"]
            names.append(arn.rsplit("/", 1)[-1])
        return names

    def deny_put_events(self, on: bool) -> None:
        doc = json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Deny", "Action": "events:PutEvents", "Resource": "*"}]})
        for role in self.roles():
            if on:
                self.iam.put_role_policy(RoleName=role, PolicyName=DENY_POLICY, PolicyDocument=doc)
            else:
                try:
                    self.iam.delete_role_policy(RoleName=role, PolicyName=DENY_POLICY)
                except self.iam.exceptions.NoSuchEntityException:
                    pass

    def consumer_events(self, order_ids: list[str]) -> dict[str, list[str]]:
        seen: dict[str, list[tuple[int, str]]] = {i: [] for i in order_ids}
        kwargs = {"logGroupName": self.log_group, "startTime": int((time.time() - 6 * 3600) * 1000)}
        while True:
            resp = self.logs.filter_log_events(**kwargs)
            for e in resp.get("events", []):
                ev = json.loads(e["message"])
                oid = ev.get("detail", {}).get("orderId")
                if oid in seen:
                    seq = int(str(ev["detail"].get("eventId", "0:0")).rsplit(":", 1)[-1])
                    if (seq, ev["detail-type"]) not in seen[oid]:  # at-least-once: dedupe on eventId
                        seen[oid].append((seq, ev["detail-type"]))
            if not resp.get("nextToken"):
                break
            kwargs["nextToken"] = resp["nextToken"]
        return {k: [t for _, t in sorted(v)] for k, v in seen.items()}

    def mock_region(self, order_id: str) -> str:
        item = self.table.get_item(Key={"pk": f"MOCK#order#SF-{order_id}", "sk": "MOCK"}).get("Item")
        return json.loads(item["v"]).get("region", "?") if item else "-"


def outbox_drill(e: Env, run: str) -> bool:
    oid = f"DRILL-{run}"
    order = json.loads((ROOT / "sample-data" / "orders.json").read_text())["orders"][0]
    order = dict(order, orderId=oid)
    print("[drill] deny events:PutEvents on the bridge roles; waiting for IAM propagation")
    e.deny_put_events(True)
    try:
        time.sleep(30)
        e.put("Order Paid", order)
        for _ in range(30):
            time.sleep(5)
            rec = e.repo.get_order(oid)
            if rec and rec.state.value != "RECEIVED":
                break
        rec = e.repo.get_order(oid)
        pending = [p["type"] for p in rec.pending_events] if rec else []
        print(f"[drill] state={rec.state.value if rec else '-'} pending={pending}")
        ok = bool(rec) and rec.state.value == "SUBMITTED" and pending == ["Fulfillment Order Submitted"]
    finally:
        e.deny_put_events(False)
    print("[drill] deny removed; waiting for IAM propagation, then running the tracking poller")
    time.sleep(30)
    poll = e.invoke("tracking")
    rec = e.repo.get_order(oid)
    print(f"[drill] poll={poll} pending_after={[p['type'] for p in rec.pending_events]}")
    return ok and poll.get("republished", 0) >= 1 and not rec.pending_events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--config", default="config/sandbox.json", help="profile whose 'project' is the stack prefix")
    ap.add_argument("--stack-prefix", default=None, help="overrides the profile's project prefix")
    ap.add_argument("--outbox-drill", action="store_true")
    ap.add_argument("--max-wait", type=int, default=2400, help="seconds to wait for every order to settle")
    args = ap.parse_args()
    prefix = args.stack_prefix or profile_project(ROOT / args.config)
    e = Env(prefix, args.env)
    run = str(int(time.time()))[-6:]
    results = {}

    for m in json.loads((ROOT / "sample-data" / "sku_map.json").read_text())["mappings"]:
        e.repo.put_mapping(m["sku"], m["marketplace"], m["msku"])
    print("inventory sync:", e.invoke("inventory"))

    if args.outbox_drill:
        results["outbox drill"] = outbox_drill(e, run)

    orders = json.loads((ROOT / "sample-data" / "orders.json").read_text())["orders"]
    ids, cancel_id = [], None
    for o in orders:
        o = dict(o, orderId=f"{o['orderId']}-{run}")
        ids.append(o["orderId"])
        if o["orderId"].startswith("100000108"):
            cancel_id = o["orderId"]
        e.put("Order Paid", o)
    print("published", len(ids), "Order Paid events")

    cancelled_sent = redriven = False
    deadline = time.time() + args.max_wait
    while time.time() < deadline:
        time.sleep(20)
        states = {i: e.state(i) for i in ids}
        if not cancelled_sent and cancel_id and states[cancel_id] == "SUBMITTED":
            e.put("Order Cancelled", {"orderId": cancel_id, "reason": "customer request (e2e)"})
            cancelled_sent = True
            print("sent Order Cancelled for", cancel_id)
        dlq_url = e.sqs.get_queue_url(QueueName=f"{e.p}-mcf-intake-dlq")["QueueUrl"]
        depth = int(e.sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["ApproximateNumberOfMessages"])
                    ["Attributes"]["ApproximateNumberOfMessages"])
        if not redriven and depth >= 2:
            q_url = e.sqs.get_queue_url(QueueName=f"{e.p}-mcf-intake")["QueueUrl"]
            moved = redrive(e.sqs, dlq_url, q_url)
            redriven = True
            print("redriven from DLQ with retry=true:", sorted(moved))
        if all(s not in ("RECEIVED", "-") for s in states.values()):
            print("tracking poll:", e.invoke("tracking"))
        states = {i: e.state(i) for i in ids}
        print("states:", states, "dlq:", depth)
        if redriven and all(s in SETTLED for s in states.values()):
            break

    print("--- final")
    for i in ids:
        r = e.repo.get_order(i)
        print(f"{i}: state={r.state.value} attempts={r.attempts} packages={len(r.packages)} "
              f"pending={len(r.pending_events)} region={e.mock_region(i)} err={r.last_error[:80]}")
    time.sleep(10)
    seen = e.consumer_events(ids)
    for i in ids:
        print(f"{i}: consumer saw {seen[i]}")
    dlq_url = e.sqs.get_queue_url(QueueName=f"{e.p}-mcf-intake-dlq")["QueueUrl"]
    print("DLQ messages:", e.sqs.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"])
    for name, ok in results.items():
        print(f"{name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
