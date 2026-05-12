"""Test fixtures for the webhook-receiver Lambda.

The handler module reads ``WEBHOOK_INBOX_QUEUE_URL`` at import time
(``QUEUE_URL = os.environ["WEBHOOK_INBOX_QUEUE_URL"]``). Setting the env
var at conftest-import time — before pytest collects/imports the test
module — lets the handler module import cleanly without an environment
restructure. Per-test ``mock.patch`` on the module's ``sqs`` attribute
then isolates each test's SQS calls.

We also prepend the Lambda's source directory to ``sys.path`` so the
test imports ``handler`` exactly the way AWS Lambda does at runtime
(``handler.handler`` resolves via a top-level ``handler`` module — there
is no ``infra.lambdas.webhook_receiver`` package on the Lambda's
``sys.path``).

Choice rationale: env-at-conftest-import + per-test ``patch("...sqs")``
is the lightest-weight pattern that keeps the production module simple
(no lazy-evaluation accessor) and still gives each test a clean mock.
An ``importlib.reload`` per test would also work but adds noise; the
``QUEUE_URL`` value is static across tests so a single import-time read
is fine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Set env vars before any test imports the handler module. The handler
# also calls ``boto3.client("sqs")`` at module load — that call needs an
# AWS region (the Lambda runtime injects ``AWS_REGION`` automatically;
# tests must do the same). The client is then replaced per-test with a
# MagicMock via the ``mock_sqs`` fixture, so no live AWS call ever fires.
os.environ.setdefault(
    "WEBHOOK_INBOX_QUEUE_URL",
    "https://sqs.test/123/treadmill-test-webhook-inbox",
)
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Make ``import handler`` work the same way the Lambda runtime does:
# the Lambda's entry point is ``handler.handler``, resolved from a
# top-level ``handler`` module sitting at the root of the asset dir.
_LAMBDA_DIR = Path(__file__).resolve().parent.parent
if str(_LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_DIR))
