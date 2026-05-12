"""Conflict-detection sweep — poll GitHub's mergeable API for open PRs.

Fires after a ``github.pr_merged`` event lands: merging one PR can cause
others on the same repo to develop conflicts against the new base. The
sweep iterates open PRs in the repo, queries GitHub's mergeable state
for each, and emits a ``github.pr_conflict`` event for any that are
detected as conflicting at HEAD.

Bunkhouse precedent: ``bunkhouse/services/api/bunkhouse/events/
consumer.py:_check_open_prs_for_conflicts`` — the GitHub API call
pattern, the ``mergeable=null`` retry-after, and the rate-limit /
graceful-degrade handling are cribbed intact and Treadmill-ized.

Per ADR-0013, this lives in the consumer/sweep layer, not in
``wf-conflict``. ``wf-conflict`` is the resolver; detection is here.

Idempotency contract
--------------------

The sweep is safe to call repeatedly. Before emitting a ``pr_conflict``
event we look for an existing one at the same ``(repo, pr_number,
head_sha)`` triple via the ``commit_sha`` column (per ADR-0014) and
skip the emit if found. The mergeability VIEW joins on
``commit_sha = head_sha`` so a single pr_conflict event per HEAD is
sufficient; a subsequent push triggers ``pr_synchronize`` with a new
HEAD which has no pr_conflict event until the next sweep observes one.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.eventbus import EventPublisher
from treadmill_api.events.github import GithubPrConflict
from treadmill_api.models import Event, TaskPR

logger = logging.getLogger("treadmill.coordination.conflict_sweep")


_NULL_MERGEABLE_RETRY_DELAY_SECONDS = 5.0
"""GitHub computes ``mergeable`` asynchronously; on the first request after
a push the field is often ``null``. Per the bunkhouse precedent we wait
this long and retry exactly once before giving up on the PR. Tests
override this with a near-zero sleep via ``monkeypatch``."""


_LIST_PRS_TIMEOUT_SECONDS = 10.0
_GET_PR_TIMEOUT_SECONDS = 10.0


async def sweep_open_prs_for_conflicts(
    session: AsyncSession,
    publisher: EventPublisher,
    github_client: Any,
    repo: str,
) -> int:
    """Query open PRs in the repo and emit ``pr_conflict`` events for those
    that are currently merge-conflicting against the base branch.

    ``github_client`` is an ``httpx.AsyncClient`` (or a stand-in that quacks
    like one — see the integration tests) pre-configured with the GitHub
    base URL and an auth token. ``None`` short-circuits the sweep with a
    log line; the caller is responsible for skipping the sweep entirely if
    GitHub credentials weren't wired at boot.

    Returns the number of newly-emitted ``github.pr_conflict`` events.
    The caller commits the session after this returns so the Event rows
    land atomically with whatever side-effect triggered the sweep.
    """
    if github_client is None:
        logger.debug(
            "conflict sweep skipped for %s: github_client is None "
            "(GITHUB_TOKEN unset at boot?)", repo,
        )
        return 0

    try:
        owner, repo_name = repo.split("/", 1)
    except ValueError:
        logger.warning(
            "conflict sweep: cannot parse owner/repo from %r; skipping", repo,
        )
        return 0

    open_pr_numbers = await _list_open_pr_numbers(
        github_client, owner, repo_name,
    )
    if not open_pr_numbers:
        logger.debug("conflict sweep: no open PRs in %s", repo)
        return 0

    logger.info(
        "conflict sweep: checking %d open PR(s) in %s",
        len(open_pr_numbers), repo,
    )

    emitted = 0
    for pr_number in open_pr_numbers:
        pr_detail = await _get_pr_detail(github_client, owner, repo_name, pr_number)
        if pr_detail is None:
            continue
        head_sha = pr_detail.get("head_sha")
        mergeable = pr_detail.get("mergeable")
        if not head_sha:
            logger.debug(
                "conflict sweep: PR #%d in %s has no head.sha; skipping",
                pr_number, repo,
            )
            continue
        if mergeable is True:
            logger.debug(
                "conflict sweep: PR #%d in %s is mergeable at %s; skipping",
                pr_number, repo, head_sha[:8],
            )
            continue
        if mergeable is None:
            logger.warning(
                "conflict sweep: PR #%d in %s mergeable still null after retry "
                "at %s; skipping (next pr_merged sweep will retry)",
                pr_number, repo, head_sha[:8],
            )
            continue

        # mergeable is False — the PR is conflicting. Idempotency probe
        # before we emit: skip if a pr_conflict event already exists at
        # this exact HEAD SHA. ADR-0014's commit_sha column is the join
        # the VIEW uses, so dedupe on that.
        if await _conflict_event_exists(session, repo, pr_number, head_sha):
            logger.debug(
                "conflict sweep: pr_conflict already emitted for PR #%d in %s "
                "at %s; skipping",
                pr_number, repo, head_sha[:8],
            )
            continue

        task_id = await _resolve_task_id(session, repo, pr_number)
        await _emit_pr_conflict(
            session, publisher, repo, pr_number, head_sha, task_id,
        )
        emitted += 1
        logger.info(
            "conflict sweep: emitted pr_conflict for PR #%d in %s at %s "
            "(task_id=%s)",
            pr_number, repo, head_sha[:8], task_id,
        )

    return emitted


# ── GitHub API calls ─────────────────────────────────────────────────────────


async def _list_open_pr_numbers(
    github_client: Any, owner: str, repo_name: str,
) -> list[int]:
    """``GET /repos/{owner}/{repo}/pulls?state=open`` → list of PR numbers.

    Returns an empty list on any error so the caller degrades gracefully
    instead of crashing the consumer loop on transient GitHub flakiness.
    Bunkhouse precedent: identical fail-soft posture.
    """
    url = f"/repos/{owner}/{repo_name}/pulls"
    try:
        response = await github_client.get(
            url,
            params={"state": "open", "per_page": 100},
            timeout=_LIST_PRS_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception(
            "conflict sweep: list-PRs failed for %s/%s", owner, repo_name,
        )
        return []

    if response.status_code != 200:
        logger.warning(
            "conflict sweep: list-PRs returned %d for %s/%s",
            response.status_code, owner, repo_name,
        )
        return []

    try:
        prs = response.json()
    except Exception:
        logger.warning(
            "conflict sweep: malformed JSON from list-PRs for %s/%s",
            owner, repo_name,
        )
        return []

    return [
        pr["number"] for pr in prs
        if isinstance(pr, dict) and isinstance(pr.get("number"), int)
    ]


async def _get_pr_detail(
    github_client: Any,
    owner: str,
    repo_name: str,
    pr_number: int,
    *,
    _retry: bool = True,
) -> dict[str, Any] | None:
    """``GET /repos/{owner}/{repo}/pulls/{n}`` → ``{head_sha, mergeable}``.

    ``mergeable`` is the GitHub API field — ``True`` / ``False`` / ``None``.
    Per the bunkhouse precedent, ``None`` means "GitHub is still computing"
    — we wait ``_NULL_MERGEABLE_RETRY_DELAY_SECONDS`` and retry exactly
    once. Returns ``None`` on any non-recoverable error so the caller
    skips the PR cleanly.
    """
    url = f"/repos/{owner}/{repo_name}/pulls/{pr_number}"
    try:
        response = await github_client.get(
            url, timeout=_GET_PR_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception(
            "conflict sweep: get-PR failed for PR #%d in %s/%s",
            pr_number, owner, repo_name,
        )
        return None

    if response.status_code == 404:
        logger.debug(
            "conflict sweep: PR #%d in %s/%s 404; treating as closed",
            pr_number, owner, repo_name,
        )
        return None
    if response.status_code != 200:
        logger.warning(
            "conflict sweep: get-PR returned %d for PR #%d in %s/%s",
            response.status_code, pr_number, owner, repo_name,
        )
        return None

    try:
        data = response.json()
    except Exception:
        logger.warning(
            "conflict sweep: malformed JSON from get-PR for PR #%d in %s/%s",
            pr_number, owner, repo_name,
        )
        return None

    head_sha = (data.get("head") or {}).get("sha")
    mergeable = data.get("mergeable")

    if mergeable is None and _retry:
        logger.debug(
            "conflict sweep: PR #%d mergeable null; retrying after %.1fs",
            pr_number, _NULL_MERGEABLE_RETRY_DELAY_SECONDS,
        )
        await asyncio.sleep(_NULL_MERGEABLE_RETRY_DELAY_SECONDS)
        return await _get_pr_detail(
            github_client, owner, repo_name, pr_number, _retry=False,
        )

    return {"head_sha": head_sha, "mergeable": mergeable}


# ── DB-side helpers ──────────────────────────────────────────────────────────


async def _conflict_event_exists(
    session: AsyncSession, repo: str, pr_number: int, head_sha: str,
) -> bool:
    """Return ``True`` iff a ``github.pr_conflict`` event was already
    emitted for this PR at this HEAD. Idempotency probe — caller skips
    the emit when this returns True.

    The probe matches on the (entity_type, action, commit_sha) triple
    plus the JSONB ``pr_number`` field to disambiguate when two PRs in
    different repos happen to share a HEAD SHA (very unlikely but
    possible across forks). The partial index on
    ``(entity_type, action, commit_sha)`` accelerates the scan.
    """
    result = await session.execute(
        select(func.count(Event.id)).where(
            Event.entity_type == "github",
            Event.action == "pr_conflict",
            Event.commit_sha == head_sha,
            Event.payload["repo"].astext == repo,
            Event.payload["pr_number"].astext == str(pr_number),
        )
    )
    return (result.scalar_one() or 0) > 0


async def _resolve_task_id(
    session: AsyncSession, repo: str, pr_number: int,
) -> uuid.UUID | None:
    """Look up the ``task_prs`` bridge row for this PR.

    Returns the task_id if the PR is tracked, else ``None``. Conflict
    events on un-tracked PRs (e.g. PRs not opened by a Treadmill worker)
    still land in the audit log with ``task_id`` NULL — the VIEW joins
    via ``task_prs.task_id`` so an untracked conflict is naturally
    inert.
    """
    result = await session.execute(
        select(TaskPR.task_id).where(
            TaskPR.repo == repo,
            TaskPR.pr_number == pr_number,
            TaskPR.closed_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _emit_pr_conflict(
    session: AsyncSession,
    publisher: EventPublisher,
    repo: str,
    pr_number: int,
    head_sha: str,
    task_id: uuid.UUID | None,
) -> None:
    """INSERT the ``github.pr_conflict`` Event row + publish to the bus.

    Mirrors the dispatcher's ``persist_and_publish`` shape but stays
    inline here because the sweep is a single-call site and the
    ``commit_sha`` column needs to be populated explicitly (the
    dispatcher's helper doesn't take it — it's for non-commit-keyed
    events). Publish failures are logged and swallowed: the Event row
    is the source of truth and the mergeability VIEW reads from the
    row, not the bus.
    """
    typed = GithubPrConflict(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        is_conflicting=True,
    )
    event = Event(
        entity_type="github",
        action="pr_conflict",
        task_id=task_id,
        payload=typed.model_dump(mode="json"),
        commit_sha=head_sha,
    )
    session.add(event)
    await session.flush()
    try:
        await publisher.publish(event, typed)
    except Exception:
        logger.exception(
            "conflict sweep: publish failed for pr_conflict "
            "(repo=%s pr=%d head=%s); Event row %s persisted",
            repo, pr_number, head_sha[:8], event.id,
        )
