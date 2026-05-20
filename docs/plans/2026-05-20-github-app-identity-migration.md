---
auto_merge: false
---

# Plan: GitHub App identity migration

- **Status:** drafting
- **Date:** 2026-05-20
- **Related ADRs:** ADR-0049 (decision + auth-flow diagram), amends ADR-0016 / ADR-0017 / ADR-0031

## Goal

Execute ADR-0049: move Treadmill off the single personal PAT to a GitHub App. The App authors/merges via short-lived per-installation tokens, webhook signing moves to the App secret, and per-repo installation becomes the on-ramp for operating on repos Treadmill did not create.

## Success criteria

- Bot commits/PRs show `treadmill[bot]`, distinct from the operator.
- The API mints installation tokens (App JWT → installation token, ~1h) and uses them for all PR/merge calls; no PAT in the API path.
- Workers authenticate `gh`/`git push` with an installation token; no PAT on worker containers. A worker run longer than the token TTL refreshes mid-run without failing.
- Webhook signatures verify against the App webhook secret.
- End-to-end still green: a task goes author → review → auto-merge, and webhook ingestion drives coordination, with zero PAT involvement.
- The personal PAT is removed from API + worker config.

## Constraints / scope

### In scope
The 8 phases below, against `joeLepper/treadmill` as the first (and initially only) installation.

### Out of scope
- GitHub-enforced required-reviews / second-identity 4-eyes (deferred per ADR-0049).
- Onboarding a *second* repo end-to-end — phase 7 builds the mechanism; exercising it on a non-Treadmill repo is the separate bootstrap track.
- Fixing the fully-local `cdk synth` papercut (unrelated).

### Budget
Roughly one focused week of implementation after the operator completes phase 1. Abort to a post-mortem if the worker mid-run token-refresh (phase 5) proves intractable rather than escalating quietly.

## Sequence of work

1. **Operator: register the App** (GitHub UI — see "What we need from the operator" below). Blocks everything; nothing else starts until App ID + private key + webhook secret + installation exist.
2. **Secrets** — store App ID, private key (PEM), and webhook secret in Secrets Manager; add config keys (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_WEBHOOK_SECRET`) alongside the existing PAT (parallel, not yet cutover).
3. **Token minting in the API** — JWT signing + `POST /app/installations/{id}/access_tokens`; cache per-installation with TTL + refresh. Unit-tested in isolation.
4. **API cutover** — replace the httpx PAT Bearer with the installation token for PR/merge; resolve installation per repo. (Depends on 3.)
5. **Worker auth swap** — worker fetches an installation token at startup for `gh auth`/`git push`; refresh before TTL for long runs. (Depends on 3.)
6. **Webhook signing cutover** — verify against the App webhook secret (amends ADR-0017); coordinate the secret swap with phase 1's webhook config.
7. **Per-repo install/onboarding flow** — record an installation for a repo and resolve `installation_id` on demand; the bootstrap on-ramp.
8. **Cutover + decommission** — remove the PAT from API + workers; verify end-to-end green; rotate/revoke the old personal PAT.

## Diagram

See the auth-flow `sequenceDiagram` in ADR-0049 (the contract of intent for phases 3–6).

## What we need from the operator (phase 1, GitHub UI)

Surfaced separately in chat so you can act on it. In short: create a GitHub App, set the permissions + event subscriptions, point its webhook at the API ingestion URL with a generated secret, generate a private key, then install it on `joeLepper/treadmill`. Hand back the **App ID** and **installation** (non-secret); place the **private key** and **webhook secret** straight into Secrets Manager (not pasted in chat).

**Multi-org requirement:** a confirmed goal is operating on repos *outside* the owner's personal org. Set the App's installation scope to **"Any account"** (not "Only on this account") so target orgs can install it. Each org's admin installs the App and approves its permissions (orgs with a third-party-application-access policy require an org *owner* to approve). One webhook URL serves all installations; events carry `installation.id`, which the per-repo onboarding flow (phase 7) resolves to the right scoped token. Optional long-term cleanup: own the App under a dedicated GitHub org rather than a personal account (Apps are transferable, so not a blocker now).

## Risks / unknowns

- **Installation-token expiry mid worker run** — chief risk. Mitigation: refresh-before-expiry in the worker; abort to post-mortem if refresh can't be made reliable.
- **Webhook-secret cutover** — a window where old/new secrets disagree drops events. Mitigation: accept both secrets briefly during cutover, then drop the old.
- **Identity change** — anything keyed on the operator's username as author breaks. Mitigation: grep for username assumptions before phase 8.

## Decisions captured during execution

(none yet)

## Post-mortem

(pending)
