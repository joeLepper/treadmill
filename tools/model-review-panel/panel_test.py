#!/usr/bin/env python3
"""Unit tests for the cross-model review panel. No network: reviewer legs are
stubbed. Run: python3 panel_test.py"""
import sys

import panel


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


# strip_reasoning removes inline <think> (MiniMax) and dangling open tags.
check("strip closed think",
      panel.strip_reasoning("<think>plan</think>VERDICT: approve") == "VERDICT: approve")
check("strip dangling think (truncated)",
      panel.strip_reasoning("visible<think>cut off mid-reason") == "visible")
check("strip reasoning tag",
      panel.strip_reasoning("<reasoning>x</reasoning>done") == "done")
check("strip leaves clean text", panel.strip_reasoning("VERDICT: block") == "VERDICT: block")

# parse_verdict is case-insensitive and finds the verdict anywhere.
check("parse block", panel.parse_verdict("VERDICT: block\n1. ...") == "block")
check("parse notes", panel.parse_verdict("verdict: Approve-With-Notes") == "approve-with-notes")
check("parse approve", panel.parse_verdict("prose\nVERDICT: approve") == "approve")
check("parse none", panel.parse_verdict("no verdict here") is None)
check("parse empty", panel.parse_verdict("") is None)

# panel_verdict takes the WORST verdict (block > notes > approve), ignores missing.
check("panel worst is block",
      panel.panel_verdict([{"verdict": "approve"}, {"verdict": "block"}, {"verdict": None}]) == "block")
check("panel notes over approve",
      panel.panel_verdict([{"verdict": "approve"}, {"verdict": "approve-with-notes"}]) == "approve-with-notes")
check("panel all approve",
      panel.panel_verdict([{"verdict": "approve"}, {"verdict": "approve"}]) == "approve")
check("panel none -> no-verdict", panel.panel_verdict([{"verdict": None}]) == "no-verdict")

# run_reviewer: a leg that raises DEGRADES (status error), never crashes the panel,
# and is never counted as an approval.
def boom():
    raise RuntimeError("provider down")


r = panel.run_reviewer("x", "gpt", boom, timeout=5)
check("degrade status error", r["status"] == "error" and "provider down" in r["error"])
check("degrade no verdict", r["verdict"] is None)

# run_reviewer: empty/whitespace output (reasoning ate the budget) -> no-output,
# NOT a silent approval.
r2 = panel.run_reviewer("y", "open-weight", lambda: "   ", timeout=5)
check("empty -> no-output", r2["status"] == "no-output" and r2["verdict"] is None)

# run_reviewer: output with no VERDICT line -> no-verdict (not counted).
r3 = panel.run_reviewer("z", "claude", lambda: "some findings but no verdict header", timeout=5)
check("missing verdict -> no-verdict", r3["status"] == "no-verdict" and r3["verdict"] is None)

# run_reviewer: a well-formed reviewer parses its verdict and keeps the review body.
r4 = panel.run_reviewer("q", "open-weight",
                        lambda: "<think>x</think>VERDICT: block\n1. [BLOCKING] bad", timeout=5)
check("good reviewer parses block", r4["verdict"] == "block" and r4["status"] == "ok")
check("good reviewer keeps body", "1. [BLOCKING] bad" in r4["review"])

# A degraded panel (all legs failed) is no-verdict, so a gate does not read silence
# as approval.
check("all-degraded -> no-verdict", panel.panel_verdict([r, r2, r3]) == "no-verdict")

print("PASS: strip, verdict parse, worst-verdict rank, and graceful degradation")
