"""The boundary type at the AWS-to-local hop for GitHub webhook delivery (ADR-0017).

Produced by the API Gateway -> Lambda receiver
(``infra/lambdas/webhook_receiver``) which writes ``{"headers": headers,
"body": body}`` JSON to the webhook-inbox SQS queue, and consumed by the
local poller (``coordination/webhook_inbox.py``, Phase C.1) which calls
``WebhookInboxEnvelope.model_validate_json(message_body)`` on every SQS
receive. Validation failure is poison-safe: log at WARNING and delete
(re-delivery would just fail again).

Per ADR-0011's "Pydantic-at-every-boundary" rule the envelope is a typed
model, not a raw dict. ``extra="forbid"`` rejects unknown top-level keys
at validation time — the discipline that catches contract drift between
the Lambda writer and the poller reader at the seam, not three layers in.

See ADR-0017 for the full contract, in particular:

* The "Pydantic boundary type" section — defines this shape.
* The "Header preservation contract" section — the Lambda preserves
  exactly three load-bearing lowercase header keys in ``headers``:

    - ``x-github-event``      - drives the ``(event, action)`` -> verb
                                mapping in ``webhooks/normalize.py``.
    - ``x-github-delivery``   - the poller derives
                                ``event_id = uuid.uuid5(NAMESPACE_OID, value)``
                                so SQS visibility-timeout redeliveries
                                collapse onto the same Event row via
                                ``ON CONFLICT (id) DO NOTHING``.
    - ``x-hub-signature-256`` - HMAC-SHA256 signature, verified by
                                ``webhooks/signatures.py``.

Adding to the preserved-header set is a one-line Lambda change plus an
ADR amendment.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WebhookInboxEnvelope(BaseModel):
    """The Lambda -> SQS -> poller envelope for a single GitHub webhook delivery.

    Shape-compatible by construction with the Lambda's
    ``envelope = {"headers": headers, "body": body}`` payload.
    """

    model_config = ConfigDict(extra="forbid")

    headers: dict[str, str]
    """Lowercase HTTP header names mapped to string values.

    Per ADR-0017's header-preservation contract the Lambda always
    includes ``x-github-event``, ``x-github-delivery``, and
    ``x-hub-signature-256``; missing or empty values for any of these is
    a poller-side reject (DLQ). All three are load-bearing — see this
    module's docstring for the role each plays.
    """

    body: str
    """The raw HTTP request body, UTF-8 decoded if the request arrived
    base64-encoded at API Gateway.

    The poller passes this through to ``verify_github_signature`` as
    bytes (``body.encode("utf-8")``); the HMAC is over the exact bytes
    GitHub signed, so the round-trip must be byte-stable.
    """
