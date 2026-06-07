"""Worker hint request tool (ADR-0081 §2).

Implements the request_hint tool that workers invoke to ask the operator
for context when stuck. The tool writes a relay file to the operator's
cc-channels inbox and POSTs an event to the API.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class RequestHintToolResult:
    """Return value from request_hint tool."""

    acknowledged: bool


class RequestHintTool:
    """Implements the request_hint tool for workers (ADR-0081 §2)."""

    def __init__(
        self,
        api_base_url: str,
        task_id: str,
        worker_step_id: str,
        created_by: str,
        api_timeout: float = 10.0,
    ) -> None:
        """Initialize the tool.

        Args:
            api_base_url: Base URL for the Treadmill API
            task_id: The task ID this step is running for
            worker_step_id: The step ID of the worker step
            created_by: The operator label (task.created_by)
            api_timeout: Timeout for API calls in seconds
        """
        self.api_base_url = api_base_url
        self.task_id = task_id
        self.worker_step_id = worker_step_id
        self.created_by = created_by
        self.api_timeout = api_timeout

    def request_hint(
        self, reason: str, context: str
    ) -> RequestHintToolResult:
        """Request operator context for the current step.

        The tool writes a relay file to the operator's inbox and POSTs an
        event to the API. It returns immediately (non-blocking) and does not
        wait for the operator to respond.

        Args:
            reason: Short slug naming the class of help (e.g.
                'tests_need_scope', 'alembic_heads_unclear').
                Max 100 chars.
            context: Brief description of what was tried and what's stuck.
                Max 2000 chars.

        Returns:
            RequestHintToolResult with acknowledged=True
        """
        # Truncate to bounds
        reason = reason[:100]
        context = context[:2000]

        # Write relay file to operator's inbox
        self._write_relay_file(reason, context)

        # POST event to API (non-blocking — ignore failures)
        try:
            self._post_event(reason, context)
        except Exception:
            # Non-blocking: if the API call fails, we still return success
            # so the worker can continue. The relay file landed, so the
            # operator can still see the request.
            pass

        return RequestHintToolResult(acknowledged=True)

    def _write_relay_file(self, reason: str, context: str) -> None:
        """Write the relay file to ~/.cc-channels/<created_by>/relay/."""
        relay_dir = (
            Path.home() / ".cc-channels" / self.created_by / "relay"
        )
        relay_dir.mkdir(parents=True, exist_ok=True)

        # Timestamp-based filename with worker ID suffix per ADR-0081 §2
        ts = int(time.time() * 1000)
        filename = (
            f"{ts}-from-worker-{self.task_id}.md"
        )
        relay_path = relay_dir / filename

        # Body format: reason header + context body + metadata footer
        body_lines = [
            f"# Worker hint request\n",
            f"\n## Reason\n\n{reason}\n",
            f"\n## Context\n\n{context}\n",
            f"\n---\n",
            f"\n[from: worker-{self.task_id}]\n",
            f"\nWorker step: {self.worker_step_id}\n",
            f"Requested at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n",
        ]
        relay_path.write_text("".join(body_lines))

    def _post_event(self, reason: str, context: str) -> None:
        """POST worker_hint_requested event to the API."""
        # Excerpt is first 500 chars per ADR-0081 §4
        context_excerpt = context[:500]

        payload = {
            "reason": reason,
            "context_excerpt": context_excerpt,
            "worker_step_id": self.worker_step_id,
        }

        url = f"{self.api_base_url}/api/v1/tasks/{self.task_id}/worker_hint_request"
        with httpx.Client(timeout=self.api_timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
