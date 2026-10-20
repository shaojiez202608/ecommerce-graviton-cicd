"""Deployed MCF bridge end-to-end check (mock SP-API): seed SKU map, publish synthetic
'Order Paid' events, run inventory sync and tracking polls, print final states.

    uv run python scripts/mcf_e2e.py --env dev [--config config/sandbox.json] [--stack-prefix <project>]
Only touches resources of the named stack; prints no account identifiers.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "mcf"))
from mcf_bridge.store import DynamoRepository  # noqa: E402


def outputs(cfn, stack):
    return {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--config", default="config/sandbox.json", help="profile whose 'project' is the stack prefix")
    ap.add_argument("--stack-prefix", default=None, help="overrides the profile's project prefix")
    ap.add_argument("--wait", type=int, default=150, help="seconds to wait for the mock to ship/deliver")
    args = ap.parse_args()
    if not args.stack_prefix:
        args.stack_prefix = json.loads((ROOT / args.config).read_text())["project"]
    stack = f"{args.stack_prefix}-{args.env}-mcf"
    out = outputs(boto3.client("cloudformation"), stack)
    table = boto3.resource("dynamodb").Table(out["StateTableName"])
    repo = DynamoRepository(table)
    lam = boto3.client("lambda")
    events = boto3.client("events")

    for m in json.loads((ROOT / "sample-data" / "sku_map.json").read_text())["mappings"]:
        repo.put_mapping(m["sku"], m["marketplace"], m["msku"])
    inv = json.loads(lam.invoke(FunctionName=f"{args.stack_prefix}-{args.env}-mcf-inventory")["Payload"].read())
    print("inventory sync:", inv)

    orders = json.loads((ROOT / "sample-data" / "orders.json").read_text())["orders"]
    run = str(int(time.time()))[-6:]
    ids = []
    for o in orders:
        o = dict(o, orderId=f"{o['orderId']}-{run}")
        ids.append(o["orderId"])
        resp = events.put_events(Entries=[{"Source": "storefront.orders", "DetailType": "Order Paid",
                                           "Detail": json.dumps(o), "EventBusName": out["EventBusName"]}])
        assert resp["FailedEntryCount"] == 0
    print("published", len(ids), "Order Paid events")

    deadline = time.time() + args.wait + 240
    tracked = False
    while time.time() < deadline:
        time.sleep(20)
        states = {i: (repo.get_order(i).state.value if repo.get_order(i) else "-") for i in ids}
        if time.time() > deadline - 240 + args.wait - 90 or all(s in ("SUBMITTED", "EXCEPTION", "COMPLETE") for s in states.values()):
            poll = json.loads(lam.invoke(FunctionName=f"{args.stack_prefix}-{args.env}-mcf-tracking")["Payload"].read())
            print("tracking poll:", poll)
            tracked = True
        print("states:", states)
        if tracked and all(s in ("COMPLETE", "EXCEPTION") for s in states.values()):
            break
    for i in ids:
        r = repo.get_order(i)
        print(f"{i}: state={r.state.value} attempts={r.attempts} packages={len(r.packages)} err={r.last_error[:70]}")
    sqs = boto3.client("sqs")
    dlq = sqs.get_queue_url(QueueName=f"{args.stack_prefix}-{args.env}-mcf-intake-dlq")["QueueUrl"]
    attrs = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
    print("DLQ messages:", attrs["ApproximateNumberOfMessages"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
