"""Plan-merge-to-main trigger handler (ADR-0021).

When the coordination consumer projects a ``github.pr_merged`` event, this
module fetches the merged PR's changed files from GitHub, filters for
plan docs (``docs/plans/*.md``), and — for each plan doc whose
frontmatter declares ``status: active`` — invokes the internal Scenario-1
plan-creation function to spawn a Plan + tasks.

The merge handler is a *different trigger* into the same execution
machinery as ``POST /plans`` with ``doc_content``. Per ADR-0021 the
``event_triggers`` machinery is *not* used here — that's for "spawn a
workflow run from an event"; this is "create a plan from a doc."

Idempotency
-----------

``plan_id`` is derived deterministically via ``uuid.uuid5`` of
``"{repo}:{path}@{merge_commit_sha}"``. SQS redelivery of the same
``pr_merged`` event therefore converges on the same Plan row; we probe
for an existing row via ``SELECT`` before inserting so the second
delivery is a no-op (rather than relying on ``ON CONFLICT`` + a rollback
of the surrounding work).

Failure modes
-------------

* ``github_client`` is ``None`` (GITHUB_TOKEN unset) — log a warning and
  return without dispatch. The CLI submission path is still available.
* Repo not in the allow-list — return silently with no events.
* PR has no plan-doc files — return silently with no events.
* Plan-doc frontmatter ``status != "active"`` — persist a
  ``plan_doc.observed_inactive`` event and skip dispatch.
* Plan-doc parse failure — persist a ``plan_doc.parse_failed`` event and
  skip dispatch. Other plan docs in the same PR are processed
  independently.
* GitHub API error (PR fetch, file fetch) — log + return; the SQS
  message stays on the queue for retry per the consumer's standard
  error semantics.
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from treadmill_api.config import Settings
from treadmill_api.dispatch import Dispatcher
from treadmill_api.events.plan_doc import (
    PlanDocObservedInactive,
    PlanDocParseFailed,
)
from treadmill_api.models import Event, Plan
from treadmill_api.parsers.plan_doc import PlanDocFormatError

logger = logging.getLogger("treadmill.coordination.plan_doc_trigger")


_PLAN_DOC_PREFIX = "docs/plans/"
"""v0 pattern per ADR-0021 §"Plan-doc path pattern": ``docs/plans/*.md``
(one level). Bump to ``**/*.md`` if plans nest (Q21.d)."""

_PLAN_DOC_SUFFIX = ".md"

_LIST_FILES_TIMEOUT_SECONDS = 10.0
_GET_CONTENTS_TIMEOUT_SECONDS = 10.0


def _is_plan_doc_path(path: str) -> bool:
    """v0 glob: exactly ``docs/plans/<name>.md`` with no further nesting."""
    if not path.startswith(_PLAN_DOC_PREFIX):
        return False
    if not path.endswith(_PLAN_DOC_SUFFIX):
        return False
    # Reject deeper nesting (``docs/plans/2026/foo.md``); per Q21.d the
    # v0 glob is one level. The split-and-count is the cheapest exact
    # check.
    remainder = path[len(_PLAN_DOC_PREFIX):]
    return "/" not in remainder


def derive_plan_id(repo: str, path: str, merge_commit_sha: str) -> uuid.UUID:
    """Deterministic plan id per ADR-0021 §"Plan identity".

    Same triple → same UUID, so SQS redelivery + webhook replay + CLI
    re-submit all converge on the same Plan row.
    """
    return uuid.uuid5(
        uuid.NAMESPACE_OID, f"{repo}:{path}@{merge_commit_sha}",
    )


def _extract_frontmatter(content: str) -> dict[str, Any] | None:
    """Return the parsed YAML frontmatter block at the top of ``content``,
    or ``None`` if no frontmatter is present.

    Recognizes the conventional ``---\\n<yaml>\\n---`` delimiter at the
    very start of the document (allowing a leading BOM / whitespace).
    Anything else (a markdown ``# Heading`` first, no fence at all)
    returns ``None`` — the caller treats that as "no status declared,"
    which falls to the inactive branch.

    Raises:
        yaml.YAMLError: if the fenced block is present but the YAML
            inside cannot be parsed. Caller treats this as a parse
            failure (the doc claimed to have frontmatter and didn't).
    """
    text = content.lstrip("﻿").lstrip()
    if not text.startswith("---"):
        return None
    # Strip the opening fence + the newline that should follow it.
    after_open = text[3:]
    # The opening ``---`` must be followed by a newline; otherwise this
    # is an h-rule mid-document, not a frontmatter fence.
    if not after_open.startswith("\n") and not after_open.startswith("\r\n"):
        return None
    after_open = after_open.lstrip("\r\n")
    # Find the closing fence — a line that is exactly ``---`` (or
    # ``---\n``). Search for the next ``\n---`` and require it to be
    # followed by a line break or EOF.
    close_idx = after_open.find("\n---")
    if close_idx < 0:
        return None
    body = after_open[:close_idx]
    raw = yaml.safe_load(body) if body.strip() else {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        # Frontmatter that parses to a scalar / list isn't usable; we
        # treat that as "no frontmatter" and let the caller fall to
        # the inactive branch. A wholly malformed YAML would have
        # raised above.
        return None
    return raw


# ── Public entry point ───────────────────────────────────────────────────────


async def handle_pr_merged(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    dispatcher: Dispatcher,
    github_client: Any | None,
    settings: Settings,
    repo: str,
    pr_number: int,
    merge_commit_sha: str | None,
    sender: str | None,
) -> int:
    """Process a ``github.pr_merged`` event for the plan-merge trigger.

    Returns the number of Plans dispatched (zero when no plan-doc files
    were touched, the repo wasn't allow-listed, the github client was
    unwired, or every touched plan-doc was inactive / parse-failed).

    Catches its own exceptions per-file so a malformed doc in one file
    doesn't poison processing of others in the same PR.
    """
    if github_client is None:
        logger.warning(
            "plan_doc_trigger: github_client unwired (GITHUB_TOKEN unset?); "
            "skipping pr_merged for %s pr=%s",
            repo, pr_number,
        )
        return 0
    if not settings.plan_merge_repo_is_allowed(repo):
        logger.debug(
            "plan_doc_trigger: repo %s not in plan-merge allow-list; "
            "skipping pr_merged for pr=%s",
            repo, pr_number,
        )
        return 0
    if not merge_commit_sha:
        logger.warning(
            "plan_doc_trigger: pr_merged for %s pr=%s missing "
            "merge_commit_sha; cannot fetch doc at merge ref",
            repo, pr_number,
        )
        return 0

    try:
        owner, repo_name = repo.split("/", 1)
    except ValueError:
        logger.warning(
            "plan_doc_trigger: cannot parse owner/repo from %r; skipping",
            repo,
        )
        return 0

    plan_doc_paths = await _list_plan_doc_paths(
        github_client, owner, repo_name, pr_number,
    )
    if not plan_doc_paths:
        logger.debug(
            "plan_doc_trigger: %s pr=%s touched no plan-docs; skipping",
            repo, pr_number,
        )
        return 0

    logger.info(
        "plan_doc_trigger: %s pr=%s touched %d plan-doc(s): %s",
        repo, pr_number, len(plan_doc_paths), plan_doc_paths,
    )

    dispatched = 0
    for path in plan_doc_paths:
        try:
            created = await _process_plan_doc_file(
                sessionmaker=sessionmaker,
                dispatcher=dispatcher,
                github_client=github_client,
                owner=owner,
                repo_name=repo_name,
                repo=repo,
                path=path,
                merge_commit_sha=merge_commit_sha,
                pr_number=pr_number,
                sender=sender,
            )
        except Exception:
            # A per-file failure here (after the parse-failed branch
            # already absorbed the predictable cases) is unexpected.
            # Log + continue so the surrounding consumer message
            # eventually deletes from SQS rather than looping forever.
            logger.exception(
                "plan_doc_trigger: unexpected error processing %s @%s "
                "(repo=%s pr=%s); continuing with next file",
                path, merge_commit_sha, repo, pr_number,
            )
            continue
        if created:
            dispatched += 1
    return dispatched


async def _process_plan_doc_file(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    dispatcher: Dispatcher,
    github_client: Any,
    owner: str,
    repo_name: str,
    repo: str,
    path: str,
    merge_commit_sha: str,
    pr_number: int,
    sender: str | None,
) -> bool:
    """Process a single plan-doc file from a merged PR.

    Returns ``True`` iff a Plan was created. ``False`` for the inactive,
    parse-failed, fetch-failed, and idempotent-skip branches.
    """
    content = await _fetch_file_content(
        github_client, owner, repo_name, path, merge_commit_sha,
    )
    if content is None:
        logger.warning(
            "plan_doc_trigger: could not fetch %s @%s in %s; skipping",
            path, merge_commit_sha, repo,
        )
        return False

    # Parse frontmatter first so we can short-circuit on inactive without
    # ever running the (more expensive) sequence_of_work parser.
    frontmatter_status: str | None = None
    try:
        frontmatter = _extract_frontmatter(content)
    except yaml.YAMLError as exc:
        await _persist_parse_failed(
            sessionmaker,
            repo=repo,
            path=path,
            merge_commit_sha=merge_commit_sha,
            pr_number=pr_number,
            error=f"frontmatter YAML invalid: {exc}",
            error_type=type(exc).__name__,
        )
        return False
    if frontmatter is not None:
        raw_status = frontmatter.get("status")
        if isinstance(raw_status, str):
            frontmatter_status = raw_status

    if frontmatter_status != "active":
        await _persist_observed_inactive(
            sessionmaker,
            repo=repo,
            path=path,
            merge_commit_sha=merge_commit_sha,
            pr_number=pr_number,
            status=frontmatter_status,
        )
        logger.info(
            "plan_doc_trigger: %s @%s status=%r; persisted "
            "plan_doc.observed_inactive (repo=%s pr=%s)",
            path, merge_commit_sha, frontmatter_status, repo, pr_number,
        )
        return False

    # Active. Derive the deterministic plan_id; probe for idempotency.
    plan_id = derive_plan_id(repo, path, merge_commit_sha)

    async with sessionmaker() as session:
        existing = await session.execute(
            select(Plan.id).where(Plan.id == plan_id)
        )
        if existing.scalar_one_or_none() is not None:
            logger.info(
                "plan_doc_trigger: plan %s already exists for %s @%s "
                "(repo=%s pr=%s); idempotent skip",
                plan_id, path, merge_commit_sha, repo, pr_number,
            )
            return False

        # Import lazily so test paths that don't exercise the merge
        # handler don't trigger the routers/plans import graph at
        # module load.
        from treadmill_api.routers.plans import create_plan_from_doc

        try:
            await create_plan_from_doc(
                session,
                dispatcher,
                repo=repo,
                doc_content=content,
                doc_path=path,
                created_by=sender,
                plan_id=plan_id,
            )
        except (PlanDocFormatError, ValidationError) as exc:
            # Roll back the in-progress session — a partial Plan row
            # plus a parse_failed event in the same transaction would
            # be inconsistent.
            await session.rollback()
            await _persist_parse_failed(
                sessionmaker,
                repo=repo,
                path=path,
                merge_commit_sha=merge_commit_sha,
                pr_number=pr_number,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
        except Exception:
            await session.rollback()
            # The caller catches and continues; re-raise so the
            # consumer's outer log captures the unexpected failure
            # with full context.
            raise
        await session.commit()

    logger.info(
        "plan_doc_trigger: dispatched plan %s from %s @%s (repo=%s pr=%s)",
        plan_id, path, merge_commit_sha, repo, pr_number,
    )
    return True


# ── GitHub API calls ─────────────────────────────────────────────────────────


async def _list_plan_doc_paths(
    github_client: Any, owner: str, repo_name: str, pr_number: int,
) -> list[str]:
    """``GET /repos/{owner}/{repo}/pulls/{n}/files`` → plan-doc paths.

    v0 reads only the first page (100 files). Plan-doc-only PRs are
    tiny so this is fine; if we ever batch-merge a multi-page PR we'll
    need pagination.
    """
    url = f"/repos/{owner}/{repo_name}/pulls/{pr_number}/files"
    try:
        response = await github_client.get(
            url,
            params={"per_page": 100},
            timeout=_LIST_FILES_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception(
            "plan_doc_trigger: list-files failed for %s/%s pr=%s",
            owner, repo_name, pr_number,
        )
        return []

    if response.status_code != 200:
        logger.warning(
            "plan_doc_trigger: list-files returned %d for %s/%s pr=%s",
            response.status_code, owner, repo_name, pr_number,
        )
        return []

    try:
        files = response.json()
    except Exception:
        logger.warning(
            "plan_doc_trigger: malformed JSON from list-files for %s/%s pr=%s",
            owner, repo_name, pr_number,
        )
        return []

    if not isinstance(files, list):
        logger.warning(
            "plan_doc_trigger: unexpected list-files shape (not a list) "
            "for %s/%s pr=%s",
            owner, repo_name, pr_number,
        )
        return []

    paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("filename")
        status_field = entry.get("status")
        # Per ADR-0021 the trigger fires on plan-doc paths regardless of
        # add/modify; the status field is informational. We filter
        # ``removed`` because GitHub still lists removed files in the
        # diff and there's nothing to fetch at the merge ref for a
        # removed file.
        if not isinstance(path, str):
            continue
        if status_field == "removed":
            continue
        if _is_plan_doc_path(path):
            paths.append(path)
    return paths


async def _fetch_file_content(
    github_client: Any, owner: str, repo_name: str, path: str, ref: str,
) -> str | None:
    """``GET /repos/{owner}/{repo}/contents/{path}?ref={sha}``.

    Decodes the base64-encoded body. Returns ``None`` on any error so
    the caller falls through to the no-dispatch branch.
    """
    url = f"/repos/{owner}/{repo_name}/contents/{path}"
    try:
        response = await github_client.get(
            url,
            params={"ref": ref},
            timeout=_GET_CONTENTS_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception(
            "plan_doc_trigger: get-contents failed for %s @%s in %s/%s",
            path, ref, owner, repo_name,
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "plan_doc_trigger: get-contents returned %d for %s @%s in %s/%s",
            response.status_code, path, ref, owner, repo_name,
        )
        return None

    try:
        body = response.json()
    except Exception:
        logger.warning(
            "plan_doc_trigger: malformed JSON from get-contents for "
            "%s @%s in %s/%s",
            path, ref, owner, repo_name,
        )
        return None

    if not isinstance(body, dict):
        logger.warning(
            "plan_doc_trigger: unexpected get-contents shape for "
            "%s @%s in %s/%s (got %s)",
            path, ref, owner, repo_name, type(body).__name__,
        )
        return None

    encoding = body.get("encoding")
    raw = body.get("content")
    if not isinstance(raw, str):
        logger.warning(
            "plan_doc_trigger: get-contents missing 'content' string for "
            "%s @%s in %s/%s",
            path, ref, owner, repo_name,
        )
        return None

    if encoding == "base64":
        try:
            decoded = base64.b64decode(raw).decode("utf-8")
        except Exception:
            logger.exception(
                "plan_doc_trigger: base64 decode failed for %s @%s in %s/%s",
                path, ref, owner, repo_name,
            )
            return None
        return decoded
    # Non-base64 encodings are unexpected from the contents endpoint; log
    # and bail. (GitHub may return ``"none"`` for files >1MB — that's a
    # plan-doc-shouldn't-happen case but we degrade cleanly.)
    logger.warning(
        "plan_doc_trigger: unsupported encoding %r for %s @%s in %s/%s",
        encoding, path, ref, owner, repo_name,
    )
    return None


# ── Event persistence helpers ────────────────────────────────────────────────


async def _persist_observed_inactive(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    repo: str,
    path: str,
    merge_commit_sha: str,
    pr_number: int,
    status: str | None,
) -> None:
    payload = PlanDocObservedInactive(
        repo=repo,
        path=path,
        merge_commit_sha=merge_commit_sha,
        status=status,
        pr_number=pr_number,
    )
    async with sessionmaker() as session:
        event = Event(
            entity_type=payload.ENTITY_TYPE,
            action=payload.ACTION,
            payload=payload.model_dump(mode="json"),
        )
        session.add(event)
        await session.commit()


async def _persist_parse_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    repo: str,
    path: str,
    merge_commit_sha: str,
    pr_number: int,
    error: str,
    error_type: str,
) -> None:
    payload = PlanDocParseFailed(
        repo=repo,
        path=path,
        merge_commit_sha=merge_commit_sha,
        pr_number=pr_number,
        error=error[:2048],
        error_type=error_type,
    )
    async with sessionmaker() as session:
        event = Event(
            entity_type=payload.ENTITY_TYPE,
            action=payload.ACTION,
            payload=payload.model_dump(mode="json"),
        )
        session.add(event)
        await session.commit()
    logger.warning(
        "plan_doc_trigger: parse failed for %s @%s (repo=%s pr=%s): %s: %s",
        path, merge_commit_sha, repo, pr_number, error_type, error,
    )
