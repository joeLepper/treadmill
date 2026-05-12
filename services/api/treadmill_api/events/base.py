"""Base class for typed event payloads.

Per ADR-0011, every read/write of ``events.payload`` goes through a
per-type Pydantic model. Subclasses declare two ClassVar markers
(``ENTITY_TYPE`` + ``ACTION``) that together identify the event type;
the registry uses them as the key.

Strict validation: ``extra="forbid"`` rejects unknown fields, so a
payload that drifts from its schema fails loudly.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class EventPayload(BaseModel):
    """Marker base for all typed event payloads."""

    model_config = ConfigDict(
        extra="forbid",
        # JSON-mode serialization: UUID → str, datetime → ISO 8601 with Z.
        # Used by ``encode_payload`` when writing to JSONB.
        ser_json_bytes="base64",
    )

    # Subclasses must set both. The registry indexes payloads by this pair.
    ENTITY_TYPE: ClassVar[str]
    ACTION: ClassVar[str]
