# Plan: Evaluator code review via the cross-model panel

- **Status:** completed
- **Date:** 2026-09-13
- **Related ADRs:** ADR-0087 (team execution / evaluator role), ADR-0105 (cross-model review), ADR-0107 (gateway + panel)

## Goal

Replace the Tapestry worker-code review — today a solo same-family Claude read by the
`evaluator-<repo>` session — with a PANEL-BACKED review: the evaluator runs the
cross-model panel on the PR diff and folds the result into its verdict, bringing
non-Anthropic voices to the code-review gate. The ADR-0087 lifecycle is unchanged:
the evaluator still returns one fixed `[verdict: approve | rework]` the coordinator
parses.

## Success criteria

1. The evaluator template instructs: on a PR handoff, write the diff to a temp file
   OUTSIDE the repo/worktree (`gh pr diff <n> > "$(mktemp)"`), run
   `panel.py review --artifact <tmpfile>`, and fold the panel verdict + findings into
   its `[verdict: approve | rework]`.
2. **Observable proof the panel ran** (closes claude#1): the evaluator's reasoning
   paragraph MUST record the panel outcome — the panel verdict and which reviewers
   returned one (or that it degraded, and why). A verdict with no panel-outcome line
   is malformed. This makes a silent regression to zero cross-model review visible.
3. **Panel BLOCK is the default driver, not unconditionally binding** (closes
   kimi#1): a panel BLOCK defaults to `rework` with the blocking findings copied into
   the remediation list; the evaluator MAY override a block it judges spurious or
   diff-injected. The evaluator is the authority; the panel informs. §9.5's
   max-cycles cap (≥3 rework → orchestrator escalation) bounds any loop.
   **Override visibility is SYMMETRIC with reduced-coverage** (Ernie re-review): a
   block-override is not a private note — the evaluator SURFACES it to the coordinator
   (relayed, with the overridden findings + justification); and for a LOAD-BEARING PR
   (schema/migration/contract/infra/security), an override of a cross-model BLOCK
   ESCALATES to the orchestrator rather than merging on the evaluator's justification
   alone. Routine PRs: surface + record is enough. This closes the last quiet path
   past a cross-model block.
4. A panel approve is NECESSARY-NOT-SUFFICIENT: the evaluator still applies its own
   holistic checks (scope/artifact discipline, repo rules, task fit) before `approve`.
5. **Coverage is measured by NON-ANTHROPIC families, not "panel failed"** (closes
   Ernie BLOCKING 1). A Go cap does NOT make the panel degrade — the gpt + claude legs
   do not use OpenCode Go, so they still meet the quorum of 2 and the panel returns a
   clean verdict while silently dropping the open-weight tier; and claude is the
   evaluator's OWN family, so a Go-capped panel = ONE non-Anthropic voice (gpt). So:
   - The panel gains a first-class coverage notion: `--author-family claude` and
     `--min-cross-model N` (default 2). It counts distinct families that returned a
     verdict EXCLUDING the author's family, and reports that count; a run with fewer
     than N is flagged `reduced-coverage` (a distinct, machine-readable marker), and
     the gate exits non-zero (fail-closed) on reduced coverage even if no BLOCK.
   - Ordering: if the panel returns a real verdict, act on it (BLOCK → criterion 3).
     Reduced coverage (incl. open-weight-tier absent on a Go cap) is a SEPARATE axis.
   - Escalation by stakes: for a LOAD-BEARING PR (the exec-in-charge set —
     schema/migration/contract/infra/security, auto_merge:false), reduced cross-model
     coverage HOLDS or escalates to the orchestrator; it does NOT merge-with-a-note.
     Routine PRs may merge but MUST surface the `reduced-coverage` marker to the
     coordinator (relayed), not bury it in a private reasoning line.
6. **Large diffs: detect INPUT truncation by size, not output finish_reason** (closes
   Ernie BLOCKING 2). A diff larger than the model context is silently truncated on
   input — the model reviews a fragment, its output fits, `finish_reason != length`,
   and the panel APPROVES a fragment. The evaluator MUST check diff size against a
   budget BEFORE the panel: over threshold → deterministically CHUNK the diff (review
   each, union findings) or record reduced coverage + escalate. Never review a fragment
   as the whole; never rely on output finish_reason for input truncation.
7. **Diff-only coverage bound stated** (Ernie Q2): a raw `gh pr diff` shows changed
   lines with little surrounding context, so the panel cannot see full functions,
   callers, or type/invariant defs elsewhere — it is strongest on diff-LOCAL
   logic/security and WEAKEST on the cross-file/semantic class. That class remains the
   same-family evaluator's job; a panel-approve is NOT full cross-model coverage and
   must not be over-trusted. (This is why the shape is necessary-not-sufficient.)
8. The evaluator stays read-only IN THE TREADMILL API sense (no POST/PATCH/PUT/DELETE
   to task_executions/task_prs/events — the single-writer invariant); writing the
   diff to a temp file outside the repo is permitted and is not an API write (closes
   kimi#2). It returns the single fixed verdict; the coordinator's parse and the §9
   approve→merge / rework→re-brief flow are untouched.
9. **Deployed, not just documented** (closes claude#2): a live evaluator session's
   rendered `CLAUDE.md` contains the panel step — the change is re-rendered to the
   running teams, verified, not left as a "document the re-render needed" note.
10. Tests pin: (a) `evaluator/CLAUDE.md.tmpl` names the panel step, the BLOCK→rework
    mapping, the override-with-justification clause, and the reduced-coverage/escalation
    clause (closes kimi#3); (b) `panel.py` `--author-family` + `--min-cross-model`
    exclude the author's family and fail closed on reduced cross-model coverage.

## Constraints / scope

### In scope
- **Panel enhancement** `tools/model-review-panel/panel.py`: `--author-family` and
  `--min-cross-model N` — count distinct families with a verdict EXCLUDING the author's
  family, report the count, emit a `reduced-coverage` marker, and fail closed
  (non-zero) when it is below N. Unit tests for it. This also RETROFITS the authoring
  reviews (the Go-cap blind spot exists there too).
- `tools/team-templates/evaluator/CLAUDE.md.tmpl`: the panel step (diff → mktemp →
  panel with `--author-family claude`), the panel-outcome line in the verdict,
  BLOCK→rework mapping WITH the evaluator override (SURFACED to the coordinator; a
  load-bearing-PR override ESCALATES to the orchestrator), the
  reduced-coverage handling (surface marker; load-bearing PR → hold/escalate), the
  large-diff size-check → chunk-or-escalate, the diff-only coverage bound, and the
  read-only clarification.
- A pin test in `tools/team-templates/tests/` covering the panel step, the mapping,
  the override clause, and the reduced-coverage/escalation clause.
- Update the /plan + /decide skills to pass `--author-family claude --min-cross-model 2`
  so the authoring reviews get the same real-coverage guarantee (retire the manual
  capacity caveat in favour of the enforced marker).
- An ADR for the decision + status notes on ADR-0087 (evaluator model extended) and
  ADR-0105 (mechanism now covers the code-review gate).
- Re-render live evaluator sessions and VERIFY a running evaluator's rendered
  `CLAUDE.md` carries the panel step (deploy is required, not documented).

### Prerequisites (Ernie Q4)
- The panel's untrusted-input hardening (codex read-only+no-tools, claude sentinel
  allowlist) MUST be landed first — a PR diff is the panel's highest untrusted-input
  use (arbitrary PR authors). DONE (commit d205828).
- Each evaluator session's env must actually hold the panel's creds
  (`~/.codex/auth.json`, the claude subscription login, the gateway master key) or the
  legs silently drop — verify at deploy; a missing cred shows up as reduced-coverage.

### Out of scope
- The coordinator lifecycle, the single-writer invariant, the §8 sibling-worker peer
  review (stays as the same-family layer — the panel is the cross-model layer).
- A HARD infra dependency on OpenCode Go (degrade, never wedge).
- Hard-gate enforcement of the AUTHORING panel review (separate follow-up).
- Per-repo rule changes.

### Budget
This session's operator-team work (direct edits, not dispatched tasks). Abort to a
post-mortem if the panel cannot be made to degrade safely under a Go cap.

## Sequence of work

1. **Panel enhancement** — `--author-family` + `--min-cross-model` (exclude author
   family, report cross-model count, `reduced-coverage` marker, fail closed) + tests.
2. **ADR** — evaluator code review is panel-backed (extends ADR-0087 §9 + ADR-0105).
3. **Evaluator template** — panel step (diff → mktemp → panel `--author-family claude`),
   panel-outcome line, BLOCK→rework + override, reduced-coverage handling + load-bearing
   escalation, large-diff size-check → chunk/escalate, diff-only bound, read-only clarify.
4. **Authoring-review retrofit** — /plan + /decide pass `--author-family claude
   --min-cross-model 2`; replace the manual capacity caveat with the enforced marker.
5. **Tests** — template pins + panel-coverage unit tests.
6. **Review + deploy** — review with the panel (`--author-family claude`) + Ernie
   co-sign; re-render live evaluator sessions and verify; report.

## Risks / unknowns

- **Silent regression to zero cross-model review** (panel breaks after ship).
  Mitigation: criterion 2 (panel-outcome line = observable proof) + criterion 5
  (escalate to the orchestrator when the panel can't run, not silent-proceed-forever).
- **Prompt-injection via the diff steering a panel verdict** — the diff is untrusted
  and inlined in the panel prompt. Mitigation: the panel legs are already sandboxed
  (codex read-only; claude no-tools), so injection can't execute; and criterion 3 lets
  the evaluator override a spurious/injected BLOCK with recorded justification.
- **False-positive panel BLOCK loops §9.** Mitigation: criterion 3 override +
  §9.5 max-cycles cap (≥3 rework → escalation).
- **Go cap degrades cross-model coverage.** Mitigation: ordered degradation
  (criterion 5) — a real verdict is acted on; no verdict → own judgment + record +
  escalate; the merge pipeline is never wedged on infra.
- **Large diffs** exceed the model context and are silently truncated on INPUT (the
  model reviews a fragment, output fits, finish_reason != length). Mitigation:
  criterion 6 — the evaluator size-checks the diff BEFORE the panel and chunks
  (union findings) or records reduced-coverage + escalates; it never relies on output
  finish_reason for input truncation.
- **Panel latency** adds to the evaluator cycle. Acceptable — bursty-but-rare role
  (ADR-0089), batches per wake.
- **We'll abort if** a Go cap would WEDGE the pipeline or a broken panel could regress
  the gate to zero silently (criteria 2 & 5 unmet).

## Decisions captured during execution

- **Coverage measured by non-Anthropic families, enforced in the panel** (panel review
  claude#1 + Ernie BLOCKING 1). A Go cap silently drops the open-weight tier while
  gpt+claude keep quorum, so "panel failed" never fires; the real metric is distinct
  non-author families that voted. Enforced via `--author-family`/`--min-cross-model`,
  which also fixes the same blind spot in the already-shipped authoring reviews.
- **Panel BLOCK informs, evaluator decides** (Ernie kimi#1): unconditional binding
  risks a false-positive/injected-block loop; the evaluator may override with recorded
  justification and remains the authority.
- **Input truncation ≠ output truncation** (Ernie BLOCKING 2): detect large diffs by
  size and chunk-or-escalate; do not trust output finish_reason.
- **Deploy is a success criterion, not a note** (panel claude#2).
- **Diff-only cross-model is weakest on cross-file/semantic issues** (Ernie Q2): that
  class stays the same-family evaluator's job; necessary-not-sufficient by design.
- **HOLD is withhold-verdict + orchestrator relay, not a coordinator-parsed outcome**
  (Ernie template review): the coordinator only routes approve→merge / rework→worker,
  so "escalate to the orchestrator" was over-claimed. Corrected: a load-bearing HOLD
  withholds the verdict (never `approve`, never a worker-mis-routing `rework`) and
  relays to the orchestrator; merge SAFETY holds (no approve → no merge), backstopped
  by the §9.6 evaluator-timeout. A first-class coordinator hold/escalate route is a
  follow-up (ADR-0108 Follow-ups) — OUT OF SCOPE here (it changes the single-writer
  lifecycle contract and needs its own careful review).

## Post-mortem

- **What worked.** Merged to main (19228a3) and deployed: all 5 live team evaluator
  renders re-rendered + verified to carry the panel step and the exact coverage flag;
  the shared template refreshed for future `treadmill team up`. Panel + Ernie two-pass
  review ran on the PLAN itself and caught the load-bearing holes before any code.
- **What surprised us.** The review (panel + Ernie) found three real invariant holes
  the first draft missed: (1) a Go cap silently drops the open-weight tier while
  gpt+claude keep quorum → "panel failed" never fires → fixed at the panel source with
  `--author-family`/`--min-cross-model` (which also retrofits the shipped authoring
  reviews); (2) a case/spelling typo in `--author-family` failed OPEN → normalize +
  validate; (3) "escalate to the orchestrator" was UNWIRED (coordinator routes rework
  to the worker) and override→rework was incoherent → corrected to a HOLD (withhold
  verdict + orchestrator relay; merge-safe; §9.6 backstop).
- **What should become an ADR/learning/rule.** ADR-0108 (panel-backed code review)
  landed. Follow-up ADR: the coordinator-side first-class hold/escalate outcome (it
  changes the single-writer lifecycle — deliberately deferred).
- **What this teaches about future plans.** Reviewing the PLAN with the panel + a
  sibling BEFORE implementing paid off — every blocking finding was cheaper to fix in
  prose than in code. "Fail closed" needs the trigger to actually fire (the Go-cap
  case) and the escalation to actually be wired (the HOLD case) — name the mechanism,
  don't assume it. Adoption note: running evaluator sessions pick up the re-rendered
  CLAUDE.md on their next restart.
