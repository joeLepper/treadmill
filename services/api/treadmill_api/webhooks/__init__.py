"""GitHub webhook ingestion per ADR-0007.

Two pure modules + the router (in ``treadmill_api.routers.webhooks``):

  * ``signatures``  — HMAC-SHA256 verification with constant-time compare.
  * ``normalize``   — GitHub event + action → internal verb + payload dict.

The router glues them together: read body, verify, parse, normalize,
validate against the Pydantic event registry, look up task_id via
task_prs, persist Event row, publish via EventPublisher.
"""

from treadmill_api.webhooks.normalize import (
    NormalizationResult,
    normalize_github_event,
)
from treadmill_api.webhooks.pending_events import (
    buffer_pending_event,
    drain_pending_events,
    pending_event_count,
    pr_pending_buffer_key,
)
from treadmill_api.webhooks.persist import persist_and_resolve_webhook_event
from treadmill_api.webhooks.signatures import (
    InvalidSignatureError,
    SignatureMissingError,
    verify_github_signature,
)


__all__ = [
    "NormalizationResult",
    "InvalidSignatureError",
    "SignatureMissingError",
    "buffer_pending_event",
    "drain_pending_events",
    "normalize_github_event",
    "pending_event_count",
    "persist_and_resolve_webhook_event",
    "pr_pending_buffer_key",
    "verify_github_signature",
]
