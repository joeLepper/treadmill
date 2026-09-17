"""Host-side router integrator — ADR-0119. A HOST entrypoint, NOT started by the api lifespan.

The ADR-0118 router runs inside the credential-isolated ``treadmill-api`` container: it DECIDES
integration (records approvals to ``verdict_applications`` and exposes them at
``GET /api/v1/integration_queue``) but cannot merge — it has no git and, deliberately, not the
operator's identity. This process is the EXECUTION half. It lives in the package for code reuse
and CI, but it is run ONLY on the rainbow HOST as the OPERATOR (a systemd unit invoking
``python -m treadmill_api.router_integrator``), NEVER by the container's lifespan. It polls the
work-list over the API and merges each approved task's PR into the plan's ``joes-agents/<slug>``
branch with the local-git merger (``integrate_task``). The push closes the PR and GitHub fires
``github.pr_merged``, which the router consumes to unblock dependents — the loop closes with no
agent turn and integration attributed to the operator.

Safety (Bert #421): the queue returns the APPROVED ``head_sha``. The integrator never merges the
PR ref's current tip blind — ``integrate_task`` fetches ``refs/pull/<n>/head`` and refuses
(``head-moved``) if the tip no longer equals the approved head, so a post-approval force-push can
never integrate unapproved content as the operator. The two unactionable candidate shapes —
``slug_valid=False`` and ``pr_number=None`` (a moved head the endpoint fail-safed to NULL) — are
ESCALATED, never silently skipped, so an approval can never strand unseen.

Auth: this process authenticates as the operator NON-INTERACTIVELY via a provisioned credential
(a git credential helper / ``gh`` token or an ssh key) available to the systemd unit — NOT an
interactive keychain. See ``AGENT.md`` for the enable runbook and the credential provisioning.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from treadmill_api.coordination.git_runner import (
    SubprocessGitRunner,
    ensure_working_clone,
    identity_env,
)
from treadmill_api.coordination.integration_merger import GitRunner, MergeOp, integrate_task

logger = logging.getLogger("treadmill.router_integrator")

_DONE = {"merged", "already-integrated"}
# result -> escalation reason for the outcomes a human must resolve. Everything else
# (fetch-failed / merge-failed / push-rejected) is infra/transient: left on the work-list and
# re-driven by the next poll (idempotent), not escalated.
_ESCALATE = {"conflict": "integration_conflict", "head-moved": "integration_stale_head"}


@dataclass(frozen=True)
class Candidate:
    task_id: str
    repo: str
    pr_number: int | None
    head_sha: str
    integration_base: str
    slug_valid: bool
    integration_branch: str | None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Candidate":
        return cls(
            task_id=str(d["task_id"]),
            repo=str(d["repo"]),
            pr_number=d.get("pr_number"),
            head_sha=str(d["head_sha"]),
            integration_base=str(d.get("integration_base") or "main"),
            slug_valid=bool(d.get("slug_valid")),
            integration_branch=d.get("integration_branch"),
        )


async def process_candidate(
    c: Candidate,
    *,
    runner_factory: Callable[[str], Awaitable[GitRunner]],
    escalate: Callable[[Candidate, str], Awaitable[None]],
    integrate: Callable[..., Awaitable[str]] = integrate_task,
) -> str:
    """Act on ONE queue candidate. Returns a short outcome tag (for logging/tests). Escalation
    and git execution are injected so this is unit-testable without real HTTP or git.

    Unactionable shapes are ESCALATED, never skipped (Bert #421): an invalid slug
    (``integration_blocked``) and a NULL pr_number — the endpoint's moved-head fail-safe —
    (``integration_stale_head``). Otherwise integrate; the ``head-moved`` return (the fetch-time
    TOCTOU guard) also escalates ``integration_stale_head``; a real conflict escalates
    ``integration_conflict``; infra results are left for the next poll to retry."""
    if not c.slug_valid or c.integration_branch is None:
        await escalate(c, "integration_blocked")
        return "blocked"
    if c.pr_number is None:
        await escalate(c, "integration_stale_head")
        return "stale-head"
    runner = await runner_factory(c.repo)
    result = await integrate(
        runner,
        MergeOp(
            repo=c.repo,
            task_head=c.head_sha,
            integration_branch=c.integration_branch,
            base=c.integration_base,
            pr_number=c.pr_number,
        ),
    )
    if result in _DONE:
        logger.info("integrated task=%s head=%s -> %s", c.task_id, c.head_sha[:12], result)
        return result
    if result in _ESCALATE:
        await escalate(c, _ESCALATE[result])
        return result
    # infra/transient — leave the approval on the work-list; next poll re-drives (idempotent).
    logger.warning(
        "integration of task=%s returned %s (infra); leaving for retry", c.task_id, result
    )
    return result


class RouterIntegrator:
    """Polls the integration work-list and integrates each candidate as the operator."""

    def __init__(
        self,
        *,
        api_url: str,
        state_dir: str,
        remote_url_template: str = "https://github.com/{repo}.git",
        poll_interval: float = 30.0,
        http: httpx.AsyncClient | None = None,
        operator_name: str | None = None,
        operator_email: str | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._state_dir = state_dir
        self._remote_url_template = remote_url_template
        self._poll_interval = poll_interval
        self._http = http or httpx.AsyncClient(timeout=30.0)
        # The OPERATOR git identity for integration commits (ADR-0119): commits must attribute to
        # the operator's gh user, NOT the treadmill-router fallback. Resolved here so a merge is
        # authored by the operator. If unset, we WARN and fall back — commits would then NOT
        # attribute to the operator (the ADR-0119 falsifier), so operating requires setting it.
        self._identity = (
            identity_env(operator_name, operator_email)
            if operator_name and operator_email
            else None
        )
        if self._identity is None:
            logger.warning(
                "router integrator: no operator git identity (ROUTER_INTEGRATOR_GIT_NAME/EMAIL) — "
                "integration commits will use the treadmill-router fallback and will NOT attribute "
                "to the operator's gh user (ADR-0119). Set them before integrating real work."
            )
        self._stopped = False

    async def _runner_factory(self, repo: str) -> GitRunner:
        remote_url = self._remote_url_template.format(repo=repo)
        path = await ensure_working_clone(repo, self._state_dir, remote_url)
        return SubprocessGitRunner(path, identity=self._identity)

    async def _escalate(self, c: Candidate, reason: str) -> None:
        """POST the operator escalation as an event (the dashboard escalation bucket reads it).
        The container decides; the host reports outcomes back through the same API."""
        try:
            await self._http.post(
                f"{self._api_url}/api/v1/events",
                json={
                    "entity_type": "task",
                    "action": "escalated_to_operator",
                    "task_id": c.task_id,
                    # head_sha is load-bearing: the queue's stuck-exclusion is by (task, head), so
                    # this escalation stops re-selection of THIS head while a fresh approval at a
                    # new head still flows (Bert #422). Without it a stale_head re-escalates every
                    # poll and the incident is un-ackable.
                    "payload": {"reason": reason, "repo": c.repo, "head_sha": c.head_sha},
                },
            )
            logger.warning("escalated task=%s reason=%s", c.task_id, reason)
        except Exception:
            logger.exception("failed to POST escalation task=%s reason=%s", c.task_id, reason)

    async def poll_once(self) -> int:
        """One sweep: fetch the queue and process each candidate with PER-CANDIDATE containment
        (a poison candidate must not stall the queue — Bert #418/#419). Returns the count seen."""
        resp = await self._http.get(f"{self._api_url}/api/v1/integration_queue")
        resp.raise_for_status()
        candidates = [Candidate.from_json(d) for d in resp.json()]
        for c in candidates:
            try:
                await process_candidate(
                    c, runner_factory=self._runner_factory, escalate=self._escalate
                )
            except Exception:
                logger.exception("integrator: candidate task=%s raised; continuing", c.task_id)
        return len(candidates)

    async def run(self) -> None:
        logger.info("router integrator: polling %s every %ss", self._api_url, self._poll_interval)
        while not self._stopped:
            try:
                await self.poll_once()
            except Exception:
                logger.exception("router integrator: poll raised; continuing")
            await asyncio.sleep(self._poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="ADR-0119 host-side router integrator")
    parser.add_argument(
        "--api-url", default=os.environ.get("TREADMILL_API_URL", "http://localhost:8088")
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get(
            "ROUTER_INTEGRATOR_STATE_DIR", os.path.expanduser("~/.treadmill/router-clones")
        ),
    )
    parser.add_argument("--poll-interval", type=float, default=30.0)
    parser.add_argument("--operator-name", default=os.environ.get("ROUTER_INTEGRATOR_GIT_NAME"))
    parser.add_argument("--operator-email", default=os.environ.get("ROUTER_INTEGRATOR_GIT_EMAIL"))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    integrator = RouterIntegrator(
        api_url=args.api_url, state_dir=args.state_dir, poll_interval=args.poll_interval,
        operator_name=args.operator_name, operator_email=args.operator_email,
    )
    asyncio.run(integrator.run())


if __name__ == "__main__":
    main()
