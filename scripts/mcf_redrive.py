"""Redrive `Order Paid` messages from the MCF intake DLQ after the cause has been fixed.

Each message is copied back to the intake queue with ``detail.retry = true`` and then deleted from
the DLQ. The bridge resets an EXCEPTION order to RECEIVED with a fresh attempt budget only when that
flag is present, so a plain replay can never resubmit an order by accident.

    uv run python scripts/mcf_redrive.py --env dev [--config config/sandbox.json] [--order-id ID ...] [--dry-run]

Only touches the named environment's queues; prints order ids, no message bodies or addresses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]


def profile_project(path: Path) -> str:
    """Project prefix from the profile (local copy, or the committed template when there is none)."""
    if not path.exists():
        path = path.with_name(path.stem + ".example.json")
    return json.loads(path.read_text())["project"]


def redrive(sqs, dlq_url: str, queue_url: str, order_ids: set[str] | None = None, dry_run: bool = False) -> list[str]:
    moved: list[str] = []
    seen: set[str] = set()
    while True:
        resp = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                   VisibilityTimeout=60)
        messages = resp.get("Messages", [])
        if not messages:
            return moved
        for msg in messages:
            if msg["MessageId"] in seen:
                return moved
            seen.add(msg["MessageId"])
            envelope = json.loads(msg["Body"])
            detail = envelope.get("detail", envelope)
            order_id = str(detail.get("orderId", "?"))
            if order_ids and order_id not in order_ids:
                sqs.change_message_visibility(QueueUrl=dlq_url, ReceiptHandle=msg["ReceiptHandle"],
                                              VisibilityTimeout=0)
                continue
            if dry_run:
                moved.append(order_id)
                continue
            detail["retry"] = True
            if "detail" in envelope:
                envelope["detail"] = detail
            sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(envelope))
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=msg["ReceiptHandle"])
            moved.append(order_id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--config", default="config/sandbox.json", help="profile whose 'project' is the name prefix")
    ap.add_argument("--order-id", action="append", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    project = profile_project(ROOT / args.config)
    sqs = boto3.client("sqs")
    queue = sqs.get_queue_url(QueueName=f"{project}-{args.env}-mcf-intake")["QueueUrl"]
    dlq = sqs.get_queue_url(QueueName=f"{project}-{args.env}-mcf-intake-dlq")["QueueUrl"]
    moved = redrive(sqs, dlq, queue, set(args.order_id) if args.order_id else None, args.dry_run)
    print(("would redrive" if args.dry_run else "redriven") + f": {len(moved)} {sorted(moved)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
