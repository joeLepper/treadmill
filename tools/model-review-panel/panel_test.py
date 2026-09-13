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
check("strip dangling reasoning (false-approve guard)",
      panel.strip_reasoning("<reasoning>VERDICT: approve") == "")
check("strip leaves clean text", panel.strip_reasoning("VERDICT: block") == "VERDICT: block")

# parse_verdict is case-insensitive and finds the verdict anywhere.
check("parse block", panel.parse_verdict("VERDICT: block\n1. ...") == "block")
check("parse notes", panel.parse_verdict("verdict: Approve-With-Notes") == "approve-with-notes")
check("parse approve", panel.parse_verdict("prose\nVERDICT: approve") == "approve")
check("parse none", panel.parse_verdict("no verdict here") is None)
check("parse empty", panel.parse_verdict("") is None)
# A malformed verdict must NOT match a valid one (word-boundary anchored).
check("parse rejects 'approved'", panel.parse_verdict("VERDICT: approved") is None)
check("parse rejects 'approve-with-notes-pending'",
      panel.parse_verdict("VERDICT: approve-with-notes-pending") is None)
check("parse exact approve-with-notes",
      panel.parse_verdict("VERDICT: approve-with-notes") == "approve-with-notes")
# Worst-of-all-matches: a quoted "approve" must NOT override a real block.
check("parse worst wins (quoted approve vs real block)",
      panel.parse_verdict('quotes "VERDICT: approve" then\nVERDICT: block') == "block")
# Line-start anchor: an inline/quoted verdict (not its own line) is NOT a verdict.
check("parse ignores inline-quoted verdict",
      panel.parse_verdict('Unable to review; the format includes "VERDICT: approve".') is None)
check("parse accepts verdict after prose lines",
      panel.parse_verdict("some reasoning here\nVERDICT: block\n1. bad") == "block")

# vendor_of maps a model to its VENDOR family (cross-family quorum).
check("vendor qwen", panel.vendor_of("qwen3.8-max") == "qwen")
check("vendor glm", panel.vendor_of("glm-5.2") == "glm")
check("vendor kimi", panel.vendor_of("kimi-k3") == "kimi")
check("vendor minimax", panel.vendor_of("minimax-m3") == "minimax")

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

# gate_exit_code FAILS CLOSED (dogfound bugs): pass (0) needs no block, a real
# verdict, AND a cross-family quorum — a lone surviving reviewer must not pass.
def rr(family, verdict):
    return {"family": family, "verdict": verdict}


check("gate two-family approve -> 0",
      panel.gate_exit_code([rr("gpt", "approve"), rr("claude", "approve")]) == 0)
check("gate two-family notes -> 0",
      panel.gate_exit_code([rr("gpt", "approve-with-notes"), rr("claude", "approve")]) == 0)
check("gate any block -> 1",
      panel.gate_exit_code([rr("gpt", "approve"), rr("claude", "block")]) == 1)
check("gate lone-approve fails quorum -> 1",
      panel.gate_exit_code([rr("gpt", "approve"), rr("claude", None)]) == 1)
check("gate single-family default quorum -> 1",
      panel.gate_exit_code([rr("gpt", "approve")]) == 1)
check("gate single-family explicit quorum 1 -> 0",
      panel.gate_exit_code([rr("gpt", "approve")], min_quorum_families=1) == 0)
check("gate all-degraded -> 1 (fail closed)",
      panel.gate_exit_code([r, r2, r3]) == 1)
# Open-weight models count as distinct vendor families, so an all-open-weight panel
# meets the cross-family quorum.
check("gate open-weight vendors meet quorum -> 0",
      panel.gate_exit_code([rr("qwen", "approve"), rr("glm", "approve")]) == 0)

print("PASS: strip, verdict parse, worst-verdict rank, degradation, fail-closed quorum gate")
