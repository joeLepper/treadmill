"""AWS Lambda webhook receiver.

Wraps the API Gateway HTTP API event into a JSON envelope and enqueues it on
the webhook-inbox SQS queue. Implements ADR-0017 §"The Lambda" verbatim.

The Lambda does NOT verify signatures; verification happens in the local
poller (per ADR-0017). This module is a pure transport adapter.
"""

import base64
import json
import os

import boto3

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["WEBHOOK_INBOX_QUEUE_URL"]
PRESERVE_HEADERS = {
    "x-github-event",
    "x-github-delivery",     # load-bearing — derives the audit-row event_id
    "x-hub-signature-256",   # load-bearing — HMAC verification
}


def handler(event, _context):
    """Wrap the API Gateway HTTP API event into a JSON envelope and enqueue."""
    headers = {
        k.lower(): v
        for k, v in (event.get("headers") or {}).items()
        if k.lower() in PRESERVE_HEADERS
    }
    body = event.get("body", "") or ""
    # API Gateway base64-encodes the body when the request is binary.
    # GitHub's webhooks are always UTF-8 JSON, but a misconfigured poster
    # could send Content-Type: application/octet-stream. Decode here so
    # the poller always sees the raw bytes the operator signed.
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8", errors="replace")
        except Exception:
            # If decode fails, pass the base64 string through; HMAC will
            # fail on the poller side, the message goes to DLQ, and an
            # operator inspects.
            pass
    envelope = {
        "headers": headers,
        "body": body,
    }
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(envelope))
    return {"statusCode": 202, "body": "queued"}
