"""Pure (DB-free) foils for the ADR-0118 verdict parser.

``parse_verdict`` is the pure core the dispatch consumer's verdict loop runs before any side
effect — it must be TOTAL (never raise on a malformed record) and must normalize the two
delivery shapes (payload as a dict, or as a JSON string off the bus). The side-effecting apply
(generation bump / approval marker, idempotency) is covered by the real-DB foils in
``test_integration_dispatch_consumer.py``; this file gates the classification alone, with no DB.
"""

from __future__ import annotations

import json

from treadmill_api.coordination.dispatch_consumer import Verdict, parse_verdict


def test_rework_verdict_from_dict_payload():
    v = parse_verdict(
        {
            "entity_type": "task",
            "action": "evaluator_verdict",
            "task_id": "t1",
            "payload": {"verdict": "rework", "head_sha": "H1", "remediation": "fix X"},
        }
    )
    assert v == Verdict(task_id="t1", decision="rework", head_sha="H1", remediation="fix X")


def test_approve_verdict_from_json_string_payload():
    # the local bus may deliver the payload as a JSON string — tolerate it.
    v = parse_verdict(
        {
            "entity_type": "task",
            "action": "evaluator_verdict",
            "task_id": "t2",
            "payload": json.dumps({"verdict": "approve", "head_sha": "H2"}),
        }
    )
    assert v is not None and v.decision == "approve" and v.head_sha == "H2"


def test_head_sha_from_decision_alias_and_commit_sha_fallback():
    # accepts `decision` as an alias for `verdict`, and top-level commit_sha as a head fallback.
    v = parse_verdict(
        {
            "entity_type": "task",
            "action": "evaluator_verdict",
            "task_id": "t3",
            "commit_sha": "TOPHEAD",
            "payload": {"decision": "approve"},
        }
    )
    assert v is not None and v.decision == "approve" and v.head_sha == "TOPHEAD"


def test_missing_head_sha_is_none_not_error():
    # head_sha absence is tolerated here — the consumer back-fills it from task_prs.
    v = parse_verdict(
        {
            "entity_type": "task",
            "action": "evaluator_verdict",
            "task_id": "t4",
            "payload": {"verdict": "rework"},
        }
    )
    assert v is not None and v.head_sha is None


def test_unknown_decision_is_dropped():
    assert (
        parse_verdict(
            {
                "entity_type": "task",
                "action": "evaluator_verdict",
                "task_id": "t5",
                "payload": {"verdict": "maybe"},
            }
        )
        is None
    )


def test_wrong_event_type_is_dropped():
    assert parse_verdict({"entity_type": "run", "action": "completed"}) is None
    assert (
        parse_verdict(
            {"entity_type": "task", "action": "ci_result", "task_id": "t", "payload": {}}
        )
        is None
    )


def test_missing_task_id_is_dropped():
    assert (
        parse_verdict(
            {"entity_type": "task", "action": "evaluator_verdict", "payload": {"verdict": "approve"}}
        )
        is None
    )


def test_malformed_json_string_payload_is_dropped_not_fatal():
    v = parse_verdict(
        {
            "entity_type": "task",
            "action": "evaluator_verdict",
            "task_id": "t6",
            "payload": "{not valid json",
        }
    )
    assert v is None  # no verdict in an unparseable payload → dropped, never raised
