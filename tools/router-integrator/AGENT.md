# Router integrator (ADR-0119) — host-side approve→integration executor

## What this is

The ADR-0118 router runs inside the `treadmill-api` container and **decides** integration — it
records each approved task to `verdict_applications` and exposes the work-list at
`GET /api/v1/integration_queue`. The container is credential-isolated (no git, no operator
identity), so it cannot merge. This tool is the **execution** half (ADR-0119): a host process,
running on rainbow **as the operator's gh identity**, that polls the work-list and merges each
approved task's PR into the plan's `joes-agents/<slug>` branch via the local-git merger.

- Code: `treadmill_api.router_integrator` (in the api package for reuse + CI; **run only on the
  host**, never by the container lifespan).
- Unit: `systemd/treadmill-router-integrator.service` (a template — not auto-enabled).

## Why host-side + operator identity

The operator's professional obligations require merges attributed to his **direct gh user**, not
`treadmill[bot]`. The container only holds the bot token; the operator's identity lives on the
host. So integration executes host-side as the operator (matching the pre-router agent
coordinator). See ADR-0119 for the full rationale, the trust flow, and the falsifier.

## Safety (do not weaken)

- The queue returns the **approved** `head_sha`. `integrate_task` fetches `refs/pull/<n>/head`
  and **refuses (`head-moved`) if the tip no longer equals the approved head** — a post-approval
  force-push can never integrate unapproved content as the operator.
- The two unactionable shapes are **escalated, never skipped**: `slug_valid=False`
  (`integration_blocked`) and `pr_number=None` — the endpoint's moved-head fail-safe —
  (`integration_stale_head`).
- Per-candidate error containment: a poison candidate does not stall the queue.

## Credential (the operator provisions this)

The unit runs headless, so it **cannot** use an interactive `gh` keychain. Provide a
non-interactive credential the unit's user can read, one of:

- a git credential helper storing a **fine-grained PAT** for `https://github.com`, scoped to the
  integrated repos with `Contents: Read and write` + `Pull requests: Read and write` (prefer this
  over a classic `repo`-scope PAT — least privilege). `git config --global credential.helper
  store` writes the token in **plaintext** to `~/.git-credentials`, so `chmod 600
  ~/.git-credentials` and treat that file as a secret; or
- an ssh deploy/user key and an `ssh` remote (set `remote_url_template` to
  `git@github.com:{repo}.git`).

This is a long-lived operator credential on rainbow — a security surface. Scope it to the repos
the router integrates, and store it readable only by the unit's user.

## Commit identity (ADR-0119 — attribution)

Distinct from the PUSH credential: the merge COMMIT's author/committer. Set
`ROUTER_INTEGRATOR_GIT_NAME` + `ROUTER_INTEGRATOR_GIT_EMAIL` (in the unit) to the operator's gh
identity, with an **email verified on the operator's GitHub account**, so GitHub attributes the
integration merge commits to the operator. If unset, the integrator warns and falls back to the
`treadmill-router` bot identity — commits then do NOT attribute to the operator (the ADR-0119
falsifier). The push credential and the commit identity are independent: a push as the operator
with a bot commit-author still mis-attributes the commit. Verified in a live spin on treadmill
(2026-09-16): unset → commit authored by `treadmill-router`; set → authored by the operator and
attributed to their gh user.

## Enable runbook

1. Confirm the api is up and reachable at `TREADMILL_API_URL` and at least one plan is
   `substrate=router` with `merge_target=feature-branch`.
2. Provision the operator credential (above); verify `git push` works as the operator by hand.
3. Install + start the unit (operator's call — begins autonomous merges). A `--user` unit dies on
   operator logout unless lingering is on, so enable linger first (headless host):
   `loginctl enable-linger "$USER" && \
    cp systemd/treadmill-router-integrator.service ~/.config/systemd/user/ && \
    systemctl --user enable --now treadmill-router-integrator`
4. Verify: `GET /api/v1/integration_queue` drains as tasks are approved, the resulting
   `joes-agents/<slug>` merge commits are authored by the **operator** (not `treadmill[bot]` —
   the ADR-0119 falsifier), and `github.pr_merged` unblocks dependents.

## Monitoring

- `journalctl --user -u treadmill-router-integrator -f` — per-candidate outcomes + escalations.
- Work-list depth: `GET /api/v1/integration_queue | length`. A sustained non-zero depth means a
  wedged or stuck integrator — investigate (the durable work-list means nothing is lost; the
  integrator re-drives on restart).
