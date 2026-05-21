---
status: drafting
---

# Plan: ramjac bootstrap smoke test

- **Status:** drafting
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0051 (operator-initiated bootstrap / client-side discovery), ADR-0050 (onboarding architecture), ADR-0049 (App auth)

## Goal

Prove the unfamiliar-repo bootstrap end-to-end on **ramjac** — the first
non-Treadmill repo — by onboarding it from an operator session *inside the
ramjac checkout* and running a first task on it. This validates the
onboarding plumbing, the GitHub App's per-repo token scoping, and the
worker-operates-on-an-external-repo path in one pass.

## Success criteria

- `treadmill-local repo onboard` run from inside `ramjac/` registers
  ramjac in the "personal" deployment: a `repo_configs` row + a persisted
  `repo_profiles` row (verifiable in Postgres).
- A first smoke task on ramjac: a worker clones ramjac **via an App
  installation token**, makes a trivial change, and opens a PR authored by
  `treadmill-agent[bot]` **on the ramjac repo**.
- That PR does **not** auto-merge — ramjac is onboarded with
  `auto_merge_blocked=true`, exercising the ADR-0050 d.5 wiring (wave 3) on a
  real external repo. (We never auto-merge into someone else's repo without an
  explicit opt-in.)

## Constraints / scope

### In scope
The operator App install; a thin onboard API endpoint; the
`treadmill-local repo onboard` CLI doing client-side discovery; registering
ramjac; one trivial smoke task on ramjac.

### Out of scope
Server-side `wf-discover` / `role-cartographer` (ADR-0051 productionization);
rich discovery; conform-mode doc seeding; adapt-mode validation; turning
auto-merge *on* for ramjac.

### Budget
One pass. If the worker can't mint a ramjac-scoped token or clone the repo,
stop and fix the App/token path before anything else — that's the true gate.

## Sequence of work

0. **[Operator] Install the GitHub App on ramjac's org.** Gate on every
   server-side step; nothing downstream works without it.
1. **Onboard API endpoint** — `POST /api/v1/onboarding/repos` accepting
   `{repo, profile, mode, auto_merge_blocked}`; persists via the merged
   `OnboardingStore` (+ `context_store` for conform docs later). API stays the
   single writer (mirrors the App-token-minting endpoint).
2. **`treadmill-local repo onboard`** — infer repo from `git remote get-url
   origin`; build a minimal `repo_profile` from the local checkout (client-side
   discovery); `recommend_mode`; POST to the endpoint with
   `auto_merge_blocked=true`.
3. **Verify registration** — `repo_configs` + `repo_profiles` rows for
   ramjac in the deployment's Postgres.
4. **First smoke task** — submit a trivial task targeting ramjac; confirm a
   worker mints a ramjac-scoped installation token, clones, branches,
   commits, and opens a PR as `treadmill-agent[bot]`.
5. **Verify the safety valve** — the PR sits open; the repo-level
   `auto_merge_blocked` skip fires in the auto-merge trigger (logs / no merge).

## Risks / unknowns

- **Per-repo token scoping.** The minting endpoint takes an optional repo and
  currently falls back to the sole installation; multi-installation resolution
  (ramjac's org ≠ the personal org) is the first thing to confirm.
- **Worker on an external repo.** First time a worker clones a repo Treadmill
  didn't create — clone URL, auth, push permissions all exercised here.
- **Abort trigger:** if step 4's token/clone fails, fix the App path before
  building more onboarding surface.

## Decisions captured during execution

- Client-side discovery for v1, server-side `wf-discover` later — see ADR-0051.

## Post-mortem

_(filled when the smoke test completes)_
