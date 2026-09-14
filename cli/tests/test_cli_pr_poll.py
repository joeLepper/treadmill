"""CLI foils for ``treadmill pr poll`` (ADR-0113 PR-state reconciler).

The dangerous direction is a FALSE state: synthesizing a merge or CI event that did
not happen, or re-emitting one already recorded. So each foil pins fail-closed
behavior (any gh non-zero → emit NOTHING) and single-flight (one poller per repo).
The poller's gh access is injected (``run_gh``) so these drive it without GitHub.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from treadmill_cli.commands import pr as pr_module
from treadmill_cli.commands.pr import GhError, _poll_lock, run_poll

REPO = "netlify/agent-runner-orchestrator"


def _cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr="",
    )


def _make_run_gh(
    *,
    token_rc: int = 0,
    pr_views: dict[int, dict] | None = None,
    suites: dict[str, list[dict]] | None = None,
):
    """A fake gh runner. Serves ``gh auth token``, ``gh pr view``, and ``gh api
    .../check-suites`` from canned data; returns exit 1 for anything unmapped so
    fail-closed paths are exercised by simply omitting an entry."""
    pr_views = pr_views or {}
    suites = suites or {}

    def run_gh(args, token):
        if args[:3] == ["auth", "token", "--user"]:
            return _cp(token_rc, "gh-token-value" if token_rc == 0 else "")
        if args[:2] == ["pr", "view"]:
            n = int(args[2])
            if n not in pr_views:
                return _cp(1, "")  # fail-closed trigger
            return _cp(0, json.dumps(pr_views[n]))
        if args[:1] == ["api"]:
            sha = args[1].split("/commits/")[1].split("/")[0]
            if sha not in suites:
                return _cp(1, "")  # fail-closed trigger
            return _cp(0, json.dumps({"check_suites": suites[sha]}))
        return _cp(1, "")

    return run_gh


class _FakeClient:
    def __init__(
        self,
        open_prs: list[dict],
        *,
        merge_resp: dict | None = None,
        ci_resp: dict | None = None,
    ) -> None:
        self._open_prs = open_prs
        self.merge_calls: list[dict] = []
        self.ci_calls: list[dict] = []
        self._merge_resp = merge_resp or {"already_ingested": False}
        self._ci_resp = ci_resp or {"already_ingested": False}

    def list_open_task_prs(self, repo: str) -> list[dict]:
        return self._open_prs

    def poll_ingest_merge(self, **kw) -> dict:
        self.merge_calls.append(kw)
        return self._merge_resp

    def poll_ingest_check_run(self, **kw) -> dict:
        self.ci_calls.append(kw)
        return self._ci_resp


def test_merged_pr_with_completed_suite_ingests_both() -> None:
    client = _FakeClient([{"pr_number": 7, "head_sha": None}])
    run_gh = _make_run_gh(
        pr_views={
            7: {
                "merged": True,
                "mergeCommitOid": "merge0abc",
                "headRefOid": "head0xyz",
                "state": "MERGED",
            }
        },
        suites={
            "head0xyz": [
                {
                    "id": 4242,
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"slug": "github-actions"},
                }
            ]
        },
    )
    summary = run_poll(REPO, "joelepper-netlify", client, run_gh=run_gh)

    assert client.merge_calls == [
        {"repo": REPO, "pr_number": 7, "merge_sha": "merge0abc"}
    ]
    assert client.ci_calls == [
        {
            "repo": REPO,
            "head_sha": "head0xyz",
            "check_suite_id": 4242,
            "conclusion": "success",
            "app_slug": "github-actions",
            "pr_number": 7,
        }
    ]
    assert summary.merges_ingested == 1
    assert summary.ci_ingested == 1
    assert summary.skipped_gh_errors == 0


def test_no_transition_emits_nothing() -> None:
    """An open, unmerged PR whose only suite is still running emits NOTHING."""
    client = _FakeClient([{"pr_number": 3}])
    run_gh = _make_run_gh(
        pr_views={
            3: {
                "merged": False,
                "mergeCommitOid": None,
                "headRefOid": "head3",
                "state": "OPEN",
            }
        },
        suites={"head3": [{"id": 9, "status": "in_progress", "conclusion": None}]},
    )
    summary = run_poll(REPO, "acct", client, run_gh=run_gh)

    assert client.merge_calls == []
    assert client.ci_calls == []
    assert summary.polled == 1
    assert summary.merges_ingested == 0
    assert summary.ci_ingested == 0


def test_gh_pr_view_error_fails_closed() -> None:
    """A failed ``gh pr view`` emits NOTHING for that PR (never a false merge/CI)."""
    client = _FakeClient([{"pr_number": 11}])
    run_gh = _make_run_gh(pr_views={})  # pr 11 unmapped → gh pr view returns exit 1
    summary = run_poll(REPO, "acct", client, run_gh=run_gh)

    assert client.merge_calls == []
    assert client.ci_calls == []
    assert summary.skipped_gh_errors == 1


def test_check_suites_error_fails_closed_but_merge_proceeds() -> None:
    """The two legs are independent: a merged PR ingests the merge even if the
    check-suites read then fails; CI emits NOTHING (fail-closed)."""
    client = _FakeClient([{"pr_number": 8}])
    run_gh = _make_run_gh(
        pr_views={
            8: {
                "merged": True,
                "mergeCommitOid": "m8",
                "headRefOid": "h8",
                "state": "MERGED",
            }
        },
        suites={},  # h8 unmapped → check-suites read returns exit 1
    )
    summary = run_poll(REPO, "acct", client, run_gh=run_gh)

    assert len(client.merge_calls) == 1
    assert client.ci_calls == []
    assert summary.merges_ingested == 1
    assert summary.skipped_gh_errors == 1


def test_token_error_aborts_and_emits_nothing() -> None:
    """A broken account credential aborts the whole poll — the poll set is never
    even read, and nothing is ingested."""
    client = _FakeClient([{"pr_number": 1}])
    run_gh = _make_run_gh(token_rc=1)
    with pytest.raises(GhError):
        run_poll(REPO, "acct", client, run_gh=run_gh)
    assert client.merge_calls == []
    assert client.ci_calls == []


def test_already_ingested_is_not_counted() -> None:
    """A re-poll: the endpoints report already_ingested, so the call is made but the
    transition is NOT counted (the coordinator does not re-process)."""
    client = _FakeClient(
        [{"pr_number": 7}],
        merge_resp={"already_ingested": True},
        ci_resp={"already_ingested": True},
    )
    run_gh = _make_run_gh(
        pr_views={
            7: {
                "merged": True,
                "mergeCommitOid": "m7",
                "headRefOid": "h7",
                "state": "MERGED",
            }
        },
        suites={
            "h7": [
                {"id": 1, "status": "completed", "conclusion": "success", "app": {"slug": "x"}}
            ]
        },
    )
    summary = run_poll(REPO, "acct", client, run_gh=run_gh)

    assert len(client.merge_calls) == 1  # the call is still made (idempotent server-side)
    assert len(client.ci_calls) == 1
    assert summary.merges_ingested == 0  # but NOT counted as a new transition
    assert summary.ci_ingested == 0


def test_only_completed_suites_with_a_conclusion_ingest() -> None:
    """Among several suites on the head, only COMPLETED ones with a conclusion ingest
    (netlify's eternal 'queued' and in-progress suites are skipped)."""
    client = _FakeClient([{"pr_number": 4}])
    run_gh = _make_run_gh(
        pr_views={
            4: {"merged": False, "mergeCommitOid": None, "headRefOid": "h4", "state": "OPEN"}
        },
        suites={
            "h4": [
                {"id": 1, "status": "queued", "conclusion": None, "app": {"slug": "netlify"}},
                {"id": 2, "status": "in_progress", "conclusion": None, "app": {"slug": "ga"}},
                {"id": 3, "status": "completed", "conclusion": "failure", "app": {"slug": "ga"}},
            ]
        },
    )
    summary = run_poll(REPO, "acct", client, run_gh=run_gh)

    assert len(client.ci_calls) == 1
    assert client.ci_calls[0]["check_suite_id"] == 3
    assert client.ci_calls[0]["conclusion"] == "failure"
    assert summary.ci_ingested == 1


def test_poll_lock_is_single_flight_per_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A second non-blocking acquire for the SAME repo yields False (skip); a
    DIFFERENT repo acquires concurrently."""
    monkeypatch.setattr(pr_module, "_POLL_LOCK_DIR", tmp_path)
    with _poll_lock(REPO, blocking=False) as first:
        assert first is True
        with _poll_lock(REPO, blocking=False) as second:
            assert second is False  # same repo held → skip
        with _poll_lock("other/repo", blocking=False) as other:
            assert other is True  # different repo → concurrent OK
