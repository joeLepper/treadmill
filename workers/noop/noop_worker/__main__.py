"""Noop worker entrypoint.

Polls the SQS queue identified by ``SQS_QUEUE_URL`` once, logs the message
body, deletes the message, and exits. With ``EXIT_AFTER_STEP=true`` (the
default for ECS Fargate parity), the container exits immediately after one
message regardless of whether more are available.

Environment:
  SQS_QUEUE_URL          — SQS queue to consume from. Required.
  AWS_ENDPOINT_URL       — endpoint override for boto3 (set by the local adapter
                            to reach the moto container; unset in real AWS).
  AWS_DEFAULT_REGION     — region for boto3.
  AWS_ACCESS_KEY_ID      — ignored by moto but required by boto3.
  AWS_SECRET_ACCESS_KEY  — ignored by moto but required by boto3.
  EXIT_AFTER_STEP        — "true" / "false". Defaults to "true".
  WAIT_TIME_SECONDS      — SQS long-poll wait. Defaults to 20.
"""

from __future__ import annotations

import logging
import os
import sys

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
log = logging.getLogger("noop")


def main() -> int:
    queue_url = os.environ.get("SQS_QUEUE_URL")
    if not queue_url:
        log.error("SQS_QUEUE_URL not set")
        return 2

    wait = int(os.environ.get("WAIT_TIME_SECONDS", "20"))
    exit_after_step = os.environ.get("EXIT_AFTER_STEP", "true").lower() not in ("false", "0", "no")

    log.info("polling %s (wait=%ds)", queue_url, wait)
    sqs = boto3.client("sqs")

    while True:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait,
        )
        messages = resp.get("Messages", [])
        if not messages:
            log.info("no message; exiting")
            return 0
        msg = messages[0]
        log.info("received: %s", msg["Body"][:200])
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        log.info("deleted; processed one message")
        if exit_after_step:
            log.info("EXIT_AFTER_STEP=true; exiting")
            return 0


if __name__ == "__main__":
    sys.exit(main())
