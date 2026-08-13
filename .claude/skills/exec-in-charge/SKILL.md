---
name: exec-in-charge
description: The routine a sibling follows when they are exec-in-charge of a Treadmill plan, or the driver of a multi-plan batch — from dispatch through merged, done right. You own the OUTCOME, not just the authoring: submitting a plan is not the end, it is the start of your job. Covers the full cycle — confirm the team actually processes the work (team-health), track progress, field coordinator escalations, sequence merges with merge-time re-verification, hold downstream gates until upstream merges, keep main green at all times, honor the hygiene gates that red a PR (codenames, created_by, agent-changes naming), and report to the operator. Use this whenever you submit a plan, take over as driver of a batch, own any dispatched Treadmill work, or work seems stalled.
---

# /exec-in-charge — the routine of driving dispatched work to merged

You authored a plan and submitted it. **That is not done.** Submitting spawns tasks in state `registered`; they only become merged code if the team picks them up, builds them, and you sequence the merges. As exec-in-charge (or batch driver), the outcome is yours. Do not hand liveness, merge discipline, or gate-sequencing back to the operator — that is the job you are holding.

This skill is the cycle you run from dispatch to merged. Phases A–G repeat; you are rarely doing only one.

## The seat

- **Exec-in-charge of a plan** — you own that plan's tasks reaching merged, its coordinator escalations, and its downstream gates.
- **Batch driver** — you own several plans at once: you field every coordinator escalation for the batch, sequence merges across plans, and loop the per-plan owners. The operator sets direction; you run the floor.

State your authority in every resume/dispatch message so the coordinator can trust it (who authorized the work + what to do). **Only resume a team when execution is authorized** — the operator lifted a stand-down or directed the work. Do not lift a stand-down on your own initiative.

## Phase A — Confirm the team is ALIVE (team-health)

Ready tasks (not `blocked`) must leave `registered` within a few minutes — to a worker label (assigned/in_progress), then `pr_open` / `pr_merged`. Nothing alerts you if they don't; you look.

1. **Is work moving?**
   ```
   treadmill task list                       # or: treadmill task list --plan <id>
   ```
   If ready tasks sit at `registered` ~10+ min with none assigned, the team is not dispatching. Diagnose.
   (List truncates UUIDs to 8 chars. Full id: `curl -s "http://localhost:8088/api/v1/tasks?plan_id=<plan-id>" | python3 -m json.tool | grep -iE '"id"|"title"'`.)

2. **Are the sessions alive?**
   ```
   systemctl --user list-units "treadmill-channel@*" --all --no-legend | grep <repo-slug>
   ```
   Expect `coordinator-<repo>`, `evaluator-<repo>`, `worker-<repo>-1..N`, all `active running`. Dead/failed → `journalctl --user -u treadmill-channel@<label> -n 40` for a crash loop.

3. **`active running` is not enough — PEEK each session.** The systemd process can be up while the Claude session inside is wedged:
   ```
   tmux capture-pane -t <session-label> -p | tail -20
   ```
   Look for:
   - **Startup channel-approval prompt** — `❯ 1. I am using this for local development / 2. Exit · Enter to confirm`. The launch never auto-answered it; the session never reached its work loop. This hangs the WHOLE team on a (re)start/reboot.
   - **Stand-down hold** — *"I am holding … I wait for 'resume'."* Past startup, deliberately idle.
   - **Error / crash trace** — a different problem; read it.

4. **Fix, for EVERY session (coordinator + evaluator + all workers):**
   - Startup prompt — confirm option 1 (local dev is correct for a dev-local deployment): `tmux send-keys -t <label> Enter`, then re-peek (the TUI takes a moment).
   - Stand-down — send the resume, then Enter as a SEPARATE keystroke:
     ```
     tmux send-keys -t <label> "RESUME — <who authorized + what to do>"
     tmux send-keys -t <label> Enter
     ```
     > CRITICAL: `send-keys "text" Enter` in one call types the text but does NOT submit it. Send `Enter` as its own call. Give the coordinator the dispatch directive (plan/task ids, operator_notes to honor, merge discipline); give workers + evaluator a simple "resume, process/evaluate as normal."

5. **Verify — do not declare it fixed until you see movement.** `treadmill task list` should show worker labels / `in_progress`, and `task.ci_result` / `github.check_run_completed` events start arriving.

## Phase B — Track progress

Watch tasks flow `registered → executing → pr_open → pr_merged`. The treadmill-events channel relays lifecycle changes for THIS session's work (filtered by `created_by`); a `catch_up="true"` event reconciles state after a reconnect — trust it over assuming silence meant no progress. When a task stalls between states, go back to Phase A.

## Phase C — Field escalations

The coordinator relays questions, blocks, and cleared-for-merge signals. You answer decisively (a recommendation, not a survey), sequence what depends on what, and unblock. Route by ownership: the plan's exec-in-charge answers that plan's design questions; the batch driver handles cross-plan coordination.

## Phase D — Merge discipline (the part that burns you if you skip it)

`auto_merge:false` on high-blast-radius plans means **human merge on sibling consensus** — you, from the driver seat, after review + evaluator approval. Before every merge:

1. **Verify ground-truth yourself — never merge on a relayed "GO" alone.** A coordinator's clearance is input, not authority to skip checking.
   ```
   gh pr view <n> --repo <owner>/<repo> --json mergeable,mergeStateStatus,state
   gh pr checks <n> --repo <owner>/<repo>          # zero failing, zero pending
   ```
   Merge only on `mergeable=MERGEABLE`, `mergeStateStatus=CLEAN`, all checks complete and green.

2. **A clearance can REVERSE. Re-verify at the MOMENT of merge.** A PR cleared minutes ago can be reverted by the evaluator, or a new failing check can land. The gap between "cleared" and "you click merge" is where the bug hides. Check again, right then.

3. **After ANY base advance, the PR's prior green is STALE.** When you merge something else into main, every other open PR's cached green ran against the OLD main — it never tested the new base. `mergeStateStatus=CLEAN` only means the repo does not force up-to-date branches; GitHub will happily let a stale PR merge, and main's push CI becomes the FIRST place the new combination runs. If it fails there, main is red. Force fresh CI on the exact post-merge content before merging:
   ```
   gh api --method PUT "repos/<owner>/<repo>/pulls/<n>/update-branch"
   ```
   Then wait for the NEW run (new head SHA) to go green — verify by run `created_at` being after the base-advancing merge, not by the cached checks. Squash-merge collapses the update commit, so branch shape is unaffected.

4. **NO RED MAIN, EVER.** After each merge, watch main's push CI on the merge commit to full green:
   ```
   gh api "repos/<owner>/<repo>/commits/<merge-sha>/check-runs" \
     --jq '.check_runs[]|select(.conclusion=="failure" or .conclusion=="cancelled" or .conclusion=="timed_out")|.name'
   ```
   Empty = green. If red → revert the merge immediately, then diagnose. A green next-merge depends on a green main now, so do not stack the next merge until this one is confirmed green.

## Phase E — Sequence cross-task and cross-plan gates

Treadmill has no cross-plan machine-edges, so **you enforce cross-plan dependencies by controlling merge order.** If plan Y must not start until plan X's tasks merge, hold Y's owner and release them with an explicit signal only when X's PRs are actually merged (verified, Phase D) — not when they are "cleared." Tell the downstream owner the exact gate ("I release your 0097 when 0095 Task 1 AND Task 2 merge; I ping you the instant they do"). Within a plan, `depends_on` blocks a task until its upstream reaches merged; confirm the block lifts (Phase A) after the upstream merges.

## Phase F — Hygiene gates that RED a PR (know them before they bite)

- **Codenames.** Never echo a denylisted client name in any doc, ADR, plan, or brief — codename it per `~/.treadmill/codenames.json` (NEVER `git add` that file). The pre-commit leak hook (`tools/dev-hooks`) and the CI `secret-leak scan` red the PR on a verbatim client name.
- **`created_by` must equal `$TREADMILL_SESSION_LABEL`** on every `plan submit` / task submit. The treadmill-events channel filter keys on it; a mismatch silences the channel for this session and misattributes failures. Read `echo $TREADMILL_SESSION_LABEL` and pass it verbatim; if the CLI warns of a disagreement, STOP and re-issue.
- **agent-changes fragment naming.** `docs/agent-changes/` fragments must be `YYYY-MM-DD-<task-short-or-PR#>[-slug].md` — the token after the date is the 8-hex task-id short form or the PR number, not a free-text word. `tools/dev-hooks` reds the PR otherwise. When you brief a rework, give the worker the CORRECT example filename, not a placeholder — a bad example in the brief propagates straight into a red check.
- **docs-current-with-pr** is blocking — a task that changes code must update the touched component's `AGENT.md` in the same PR.

## Phase G — Report to the operator

Relay level is `quiet` by default (ADR-0071). Relay only the **significant** set to the operator (via Telegram if a chat is active): `pr_merged` (clean terminal success) and any unexpected terminal state (terminal_step_failure, cap_reached, gate_broken, architect amend-exhausted, unresolved conflict, cancelled). Relay structured facts (entity/action/ids), never raw event prose. Skip everything else — no firehose. Reports to Joe use ASD-STE100 Simplified Technical English.

## When NOT to intervene

If tasks are moving and the coordinator is dispatching, do not nudge for the sake of it — verify and let it run. Intervene when work stalls (Phase A), when an escalation needs a decision (Phase C), or at a merge/gate you own (Phases D–E). Over-nudging a working team wastes cycles and muddies the coordinator's queue.

## Anti-patterns (each is a real burn)

- **"Merged" ≠ "done."** Dispatching creates tasks; merging lands code. Neither is "the team ran it" or "it is deployed" — confirm the next state actually happened.
- **Merging on an early clearance.** A cleared-for-merge from minutes ago is not clearance now (an evaluator reversed #379 after the coordinator cleared it). Re-verify at merge-time.
- **Trusting cached green after a base moved.** A PR that was green before you merged something else is stale — it never tested the new main. Re-run fresh CI.
- **Merging on a relayed "GO" without checking ground-truth.** The relay is input; `gh pr view`/`checks` is the authority.
- **Reading `active running` as "the team works."** systemd up ≠ the Claude session inside reached its work loop. Peek it.
- **A bad example in a rework brief.** Handing a worker a placeholder filename/value propagates into a red check — give the real one.

## Root fixes to file (via /learning or /decide if these recur)

- The launch config should **auto-answer the startup channel-approval prompt** so the team never hangs on a reboot; the recurring hang compounds any crash-loop, since each restart re-hits the prompt.
- A **stalled-team alert** — "N ready tasks at `registered` for > T minutes" — would surface a wedged team automatically instead of a human noticing a quiet period.
