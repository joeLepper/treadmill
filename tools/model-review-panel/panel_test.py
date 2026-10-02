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

# Cross-model coverage (Go-cap blind spot): the author's own family is EXCLUDED, and
# reduced coverage fails closed even on an all-approve panel that meets the quorum.
capped = [rr("gpt", "approve"), rr("claude", "approve")]      # Go cap: open-weight dropped
full = [rr("gpt", "approve"), rr("qwen", "approve"), rr("claude", "approve")]
check("xmf excludes author family", panel.cross_model_families(capped, "claude") == {"gpt"})
check("reduced when 1 < 2", panel.reduced_coverage(capped, "claude", 2) is True)
check("not reduced when 2 >= 2", panel.reduced_coverage(full, "claude", 2) is False)
check("reduced off when threshold None", panel.reduced_coverage(capped, "claude", None) is False)
check("gate fails closed on Go-cap reduced coverage (approve+quorum but 1 non-author)",
      panel.gate_exit_code(capped, min_quorum_families=2, author_family="claude", min_cross_model=2) == 1)
check("gate passes with full cross-model coverage",
      panel.gate_exit_code(full, min_quorum_families=2, author_family="claude", min_cross_model=2) == 0)
check("coverage check inert when not requested (backward compat)",
      panel.gate_exit_code(capped, min_quorum_families=2) == 0)
# author-family match is case/whitespace-insensitive so a typo cannot fail open.
check("xmf normalizes case (Claude == claude)",
      panel.cross_model_families(capped, "Claude") == {"gpt"})
check("xmf normalizes whitespace", panel.cross_model_families(capped, " claude ") == {"gpt"})
# validate_coverage_args fails closed on misconfig (Ernie fail-open).
check("validate: min-cross-model without author-family errors",
      panel.validate_coverage_args(None, 2) is not None)
check("validate: unknown author-family errors (fails open otherwise)",
      panel.validate_coverage_args("anthropic", 2) is not None)
check("validate: mis-cased known family is accepted",
      panel.validate_coverage_args("Claude", 2) is None)
check("validate: valid config ok", panel.validate_coverage_args("claude", 2) is None)
check("validate: no coverage flags ok", panel.validate_coverage_args(None, None) is None)

# infer_kind: a decision record is recognised from its path so it is not judged as code.
check("infer adr from docs path", panel.infer_kind("docs/adrs/0010-openapi.md") == "adr")
check("infer plan from docs path", panel.infer_kind("docs/plans/2026-10-01-x.md") == "plan")
check("infer adr from bare adrs dir", panel.infer_kind("/tmp/adrs/0001-x.md") == "adr")
check("infer code for source file", panel.infer_kind("src/loop/runner.ts") == "code")
check("infer code for a diff tmp", panel.infer_kind("/tmp/review-artifact.XXaa.diff") == "code")
# FAIL-OPEN GUARD: a CODE file under a plans/ or adrs/ dir must stay 'code', so its
# implementation bugs still BLOCK (a .md requirement, not just the dir name).
check("code under plans/ stays code", panel.infer_kind("src/plan/planner.py") == "code")
check("code under plans/ (plural) stays code", panel.infer_kind("services/plans/run.ts") == "code")
check("code under adrs/ stays code", panel.infer_kind("services/adr/render.py") == "code")
check("non-md under adrs/ stays code", panel.infer_kind("docs/adrs/diagram.svg") == "code")
check("a .diff under plans/ stays code", panel.infer_kind("repo/plans/x.diff") == "code")
# root-relative decision docs infer correctly (consistent with the helper's --infer-from path)
check("root-relative adrs/*.md -> adr", panel.infer_kind("adrs/0001.md") == "adr")
check("root-relative plans/*.md -> plan", panel.infer_kind("plans/2026-x.md") == "plan")

# build_prompt: decision kinds get the DECISION addendum (mechanism gaps NON-BLOCKING); code/diff do not.
_MARK = "implementation MECHANISM is NON-BLOCKING"
check("adr prompt has decision addendum", _MARK in panel.build_prompt("docs/adrs/0010.md", "body", "adr"))
check("plan prompt has decision addendum", _MARK in panel.build_prompt("p.md", "body", "plan"))
check("design prompt has decision addendum", _MARK in panel.build_prompt("d.md", "body", "design"))
check("code prompt has NO decision addendum", _MARK not in panel.build_prompt("a.py", "body", "code"))
check("diff prompt has NO decision addendum", _MARK not in panel.build_prompt("a.diff", "body", "diff"))
check("default kind is code (no addendum)", _MARK not in panel.build_prompt("a.py", "body"))
check("every decision kind is a valid kind", panel.DECISION_KINDS <= panel.VALID_KINDS)
# The addendum must sit BEFORE the output-format block so it cannot break the output shape.
_adr_prompt = panel.build_prompt("docs/adrs/0010.md", "body", "adr")
check("addendum precedes the output-format block",
      _adr_prompt.index(_MARK) < _adr_prompt.index("Output EXACTLY this shape"))
check("decision prompt still ends with the format block (shape intact)",
      "Output EXACTLY this shape" in _adr_prompt
      and _adr_prompt.index("Output EXACTLY this shape") > _adr_prompt.index("Rules:"))

print("PASS: strip, verdict parse, worst-verdict rank, degradation, fail-closed quorum gate, artifact-kind rubric")
