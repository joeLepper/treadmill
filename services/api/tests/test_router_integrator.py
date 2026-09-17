"""Foils for the ADR-0119 host-side router integrator — pure/mocked (no real git, HTTP, or DB).

Pins the outcome routing of ``process_candidate`` (the safety-critical map from a queue candidate
to integrate-or-escalate) and the per-candidate containment of ``poll_once``. The git sequence
itself (PR-ref fetch + tip verify) is foil-tested in ``test_integration_merger.py``.
"""

from __future__ import annotations

import pytest

from treadmill_api.router_integrator import Candidate, RouterIntegrator, process_candidate


def _cand(**kw):
    base = dict(
        task_id="t1",
        repo="joeLepper/treadmill",
        pr_number=7,
        head_sha="deadbeef" * 5,
        integration_base="main",
        slug_valid=True,
        integration_branch="joes-agents/2026-09-13-demo",
    )
    base.update(kw)
    return Candidate(**base)


class _Recorder:
    def __init__(self):
        self.escalations: list[tuple[str, str]] = []

    async def escalate(self, c: Candidate, reason: str) -> None:
        self.escalations.append((c.task_id, reason))


async def _runner_factory(repo):  # never used when integrate is stubbed
    return object()


def _integrate_returning(value):
    async def _integrate(runner, op):
        _integrate.op = op  # capture for assertion
        return value
    return _integrate


@pytest.mark.asyncio
async def test_invalid_slug_escalates_blocked_without_integrating():
    rec = _Recorder()
    integ = _integrate_returning("merged")
    out = await process_candidate(
        _cand(slug_valid=False, integration_branch=None),
        runner_factory=_runner_factory, escalate=rec.escalate, integrate=integ,
    )
    assert out == "blocked"
    assert rec.escalations == [("t1", "integration_blocked")]
    assert not hasattr(integ, "op")  # integration never attempted


@pytest.mark.asyncio
async def test_null_pr_number_escalates_stale_head():
    rec = _Recorder()
    integ = _integrate_returning("merged")
    out = await process_candidate(
        _cand(pr_number=None),
        runner_factory=_runner_factory, escalate=rec.escalate, integrate=integ,
    )
    assert out == "stale-head"
    assert rec.escalations == [("t1", "integration_stale_head")]
    assert not hasattr(integ, "op")


@pytest.mark.asyncio
async def test_merged_does_not_escalate_and_passes_approved_head():
    rec = _Recorder()
    integ = _integrate_returning("merged")
    out = await process_candidate(
        _cand(), runner_factory=_runner_factory, escalate=rec.escalate, integrate=integ
    )
    assert out == "merged" and rec.escalations == []
    # the approved head + pr_number are handed to the merger (so it can PR-ref-verify)
    assert integ.op.task_head == "deadbeef" * 5 and integ.op.pr_number == 7


@pytest.mark.asyncio
async def test_conflict_escalates_conflict():
    rec = _Recorder()
    out = await process_candidate(
        _cand(), runner_factory=_runner_factory, escalate=rec.escalate,
        integrate=_integrate_returning("conflict"),
    )
    assert out == "conflict" and rec.escalations == [("t1", "integration_conflict")]


@pytest.mark.asyncio
async def test_head_moved_escalates_stale_head():
    rec = _Recorder()
    out = await process_candidate(
        _cand(), runner_factory=_runner_factory, escalate=rec.escalate,
        integrate=_integrate_returning("head-moved"),
    )
    assert out == "head-moved" and rec.escalations == [("t1", "integration_stale_head")]


@pytest.mark.asyncio
async def test_infra_result_is_left_for_retry_no_escalation():
    rec = _Recorder()
    out = await process_candidate(
        _cand(), runner_factory=_runner_factory, escalate=rec.escalate,
        integrate=_integrate_returning("push-rejected"),
    )
    assert out == "push-rejected" and rec.escalations == []  # retried next poll, not escalated


# ── poll_once containment ──────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StubHttp:
    def __init__(self, payload):
        self._payload = payload
        self.posts: list[dict] = []

    async def get(self, url):
        return _Resp(self._payload)

    async def post(self, url, json=None):
        self.posts.append(json)
        return _Resp({})


@pytest.mark.asyncio
async def test_poll_once_contains_a_poison_candidate_and_continues():
    # two candidates; the first raises inside integration, the second must still be processed.
    payload = [
        {"task_id": "bad", "repo": "r", "head_sha": "h1", "integration_base": "main",
         "slug_valid": True, "integration_branch": "joes-agents/x", "pr_number": 1},
        {"task_id": "good", "repo": "r", "head_sha": "h2", "integration_base": "main",
         "slug_valid": False, "integration_branch": None, "pr_number": None},
    ]
    http = _StubHttp(payload)
    integrator = RouterIntegrator(api_url="http://x", state_dir="/tmp/x", http=http)

    async def boom(repo):
        raise RuntimeError("clone blew up")

    integrator._runner_factory = boom  # first candidate (valid) will raise in integration
    n = await integrator.poll_once()
    assert n == 2
    # the poison one was contained; the second (invalid slug) still escalated.
    assert any(p["task_id"] == "good" and p["payload"]["reason"] == "integration_blocked"
               for p in http.posts)


# ── operator commit identity (ADR-0119 identity fix) ───────────────────────────


def test_operator_identity_resolved_from_name_and_email():
    from treadmill_api.coordination.git_runner import identity_env

    ri = RouterIntegrator(api_url="http://x", state_dir="/tmp/x",
                          operator_name="Joe Lepper", operator_email="joe@example.com")
    assert ri._identity == identity_env("Joe Lepper", "joe@example.com")
    assert ri._identity["GIT_AUTHOR_EMAIL"] == "joe@example.com"
    assert ri._identity["GIT_COMMITTER_NAME"] == "Joe Lepper"


def test_missing_operator_identity_is_permissive_in_init_but_run_hard_fails():
    # __init__ stays permissive (unit tests drive process_candidate with an injected runner):
    ri = RouterIntegrator(api_url="http://x", state_dir="/tmp/x")
    assert ri._identity is None
    # a name without an email (or vice versa) is treated as unset — both are required.
    ri2 = RouterIntegrator(api_url="http://x", state_dir="/tmp/x", operator_name="Joe Lepper")
    assert ri2._identity is None


@pytest.mark.asyncio
async def test_run_refuses_to_start_without_operator_identity():
    # HARD-FAIL (Bert #426): a real poll loop must never silently merge as the treadmill-router
    # bot. run() raises rather than warn-and-fallback, so systemd surfaces it and nothing merges.
    ri = RouterIntegrator(api_url="http://x", state_dir="/tmp/x")  # no operator identity
    with pytest.raises(RuntimeError, match="operator git identity"):
        await ri.run()
