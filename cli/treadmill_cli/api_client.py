"""Thin HTTP client for the Treadmill API."""

from __future__ import annotations

from typing import Any

import httpx

from treadmill_cli.config import CliConfig


class ApiError(Exception):
    """Raised when the API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ApiClient:
    def __init__(self, config: CliConfig, timeout: float = 30.0) -> None:
        headers = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.Client(
            base_url=config.api_url, headers=headers, timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise ApiError(response.status_code, detail)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ── Plans ─────────────────────────────────────────────────────────────────

    def create_plan(
        self,
        repo: str,
        *,
        intent: str | None = None,
        doc_path: str | None = None,
        doc_content: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"repo": repo}
        if intent is not None:
            body["intent"] = intent
        if doc_path is not None:
            body["doc_path"] = doc_path
        if doc_content is not None:
            body["doc_content"] = doc_content
        if created_by is not None:
            body["created_by"] = created_by
        return self._request("POST", "/api/v1/plans", json=body)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/plans/{plan_id}")

    def list_plan_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/v1/plans/{plan_id}/tasks")

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/tasks/{task_id}")

    def list_tasks(
        self,
        *,
        repo: str | None = None,
        plan_id: str | None = None,
        derived_status: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if repo is not None:
            params["repo"] = repo
        if plan_id is not None:
            params["plan_id"] = plan_id
        if derived_status is not None:
            params["derived_status"] = derived_status
        return self._request("GET", "/api/v1/tasks", params=params)

    def create_task(
        self,
        plan_id: str,
        title: str,
        workflow: str,
        *,
        description: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "plan_id": plan_id, "title": title, "workflow": workflow,
        }
        if description is not None:
            body["description"] = description
        if created_by is not None:
            body["created_by"] = created_by
        return self._request("POST", "/api/v1/tasks", json=body)

    def retry_task(
        self,
        task_id: str,
        reason: str,
        *,
        workflow: str | None = None,
        force_bypass_cap: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"reason": reason}
        if workflow is not None:
            # API expects ``workflow_id`` in TaskRetryRequest (routers/tasks.py).
            # Previously sent ``workflow`` which the API silently ignored,
            # making ``--workflow`` a no-op for terminal-task retries (where
            # infer_retry_workflow can't fall back to a non-terminal run).
            body["workflow_id"] = workflow
        if force_bypass_cap:
            body["force_bypass_cap"] = True
        return self._request("POST", f"/api/v1/tasks/{task_id}/retry", json=body)

    def set_operator_note(
        self,
        task_id: str,
        note: str | None,
    ) -> dict[str, Any]:
        """Set or clear the operator_note on a task (ADR-0081 §1)."""
        body: dict[str, Any] = {"note": note}
        return self._request("POST", f"/api/v1/tasks/{task_id}/operator_note", json=body)

    # ── Token meter (ADR-0089) ────────────────────────────────────────────────

    def list_task_executions(self, *, worker_label: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/api/v1/task_executions", params={"worker_label": worker_label},
        )

    def list_harvest_cursors(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/llm_calls/harvest_cursors")

    def harvest_llm_calls(
        self,
        *,
        transcript_path: str,
        byte_offset: int,
        malformed_lines: int,
        calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """``malformed_lines`` is the CUMULATIVE per-file count (overwritten
        server-side), keeping the POST retry-idempotent."""
        return self._request(
            "POST",
            "/api/v1/llm_calls/harvest",
            json={
                "transcript_path": transcript_path,
                "byte_offset": byte_offset,
                "malformed_lines": malformed_lines,
                "calls": calls,
            },
        )

    def token_report(self, *, since: str) -> dict[str, Any]:
        return self._request(
            "GET", "/api/v1/llm_calls/report", params={"since": since},
        )

    # ── PR-state poller (ADR-0113) ────────────────────────────────────────────

    def list_open_task_prs(self, repo: str) -> list[dict[str, Any]]:
        """The repo's OPEN task_prs — the poll set. Each row carries pr_number +
        head_sha the poller needs to check merge + CI on GitHub."""
        data = self._request(
            "GET", "/api/v1/task_prs", params={"repo": repo, "open": "true"},
        )
        return data.get("task_prs", [])

    def poll_ingest_merge(
        self, *, repo: str, pr_number: int, merge_sha: str,
    ) -> dict[str, Any]:
        """Ingest an observed PR merge (ADR-0113 merge leg). Idempotent server-side."""
        return self._request(
            "POST",
            "/api/v1/github/poll-ingest",
            json={
                "repo": repo,
                "pr_number": pr_number,
                "action": "pr_merged",
                "merge_sha": merge_sha,
            },
        )

    def poll_ingest_check_run(
        self,
        *,
        repo: str,
        head_sha: str,
        check_suite_id: int,
        conclusion: str,
        app_slug: str,
        pr_number: int | None = None,
    ) -> dict[str, Any]:
        """Ingest an observed completed check SUITE (ADR-0113 CI leg). Idempotent;
        a changed conclusion re-emits, a same-conclusion re-poll is a no-op."""
        body: dict[str, Any] = {
            "repo": repo,
            "head_sha": head_sha,
            "check_suite_id": check_suite_id,
            "conclusion": conclusion,
            "app_slug": app_slug,
        }
        if pr_number is not None:
            body["pr_number"] = pr_number
        return self._request(
            "POST", "/api/v1/github/poll-ingest/check-run", json=body,
        )

    # ── Onboarding ───────────────────────────────────────────────────────────

    def get_repo_config(self, repo: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/onboarding/repos/{repo}")

    def upsert_repo_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/onboarding/repos", json=config)

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def ready(self) -> dict[str, Any]:
        return self._request("GET", "/health/ready")
