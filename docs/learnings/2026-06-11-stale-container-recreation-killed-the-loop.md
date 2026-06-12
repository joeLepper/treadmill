# Stale-container recreation killed the loop for 9 hours

- **Date:** 2026-06-11
- **Severity:** dev-loop outage, 06:52:46Z → 15:47:34Z (8h54m, bounded by the
  events-table gap)
- **Authors:** treadmill-carla (recovery + window bounding), treadmill-alan
  (parallel recovery + the actor question)

## What happened

At 06:52:50Z an as-yet-unidentified actor recreated the `treadmill-api`
container using a STALE image layer — one predating that day's migrations
(`20260611_0600_harvested_llm_calls` et al.). The dev DB was already stamped
at `0600` by newer code, so the recreated container crash-looped at startup:
`alembic.util.exc.CommandError: Can't locate revision identified by
'20260611_0600'`. The events table shows zero events for the next 8h54m —
the coordinator's lifecycle writes and all event persistence were dark. The
loop's in-flight work (the evaluator-approved #1364 merge, the smoke-task
dispatch) froze in place; nothing was lost, everything resumed on recovery
(the coordinator's §2 startup-recovery sweep is built for exactly this).

Recovery: recreation from the CURRENT image (which carries the full migration
chain including the `0500` prod_promotions revert). Two operators raced the
same fix (~15:47Z, `treadmill-local up --no-build` and a manual
inspect→rm→run); one healthy container stood, no harm. Note: `docker start`
does NOT fix this class — it reuses the stale layer (confirmed; same crash).

## The lesson(s)

1. **Container-vs-image drift is the deploy-watcher-stale-source class in a
   new coat** (see `2026-05-26` learnings): a long-lived container silently
   diverges from its image tag as the tag is rebuilt. Any restart-shaped
   action then runs OLD code against NEW state. After migration-bearing
   merges, the only safe verb is RECREATE, never start/restart.
2. **The recreating actor is the open bug**: something recreated the
   container at 06:52:50Z with the stale image. Candidates: a deploy-watcher
   regression, a restart policy interacting with an OOM/crash, a manual
   action. Finding it is the durable fix; until then the class can recur.
3. **The events-table gap is the impact bound** — one SQL window query gave
   the exact outage envelope. Worth remembering as the standard first move
   for any loop-outage post-mortem.

## Follow-ups

- [ ] Identify the 06:52:50Z recreation actor — owner: first sibling with
      cycles. PARTIALLY ADDRESSED by task c62f097d (digest-pinned recreate):
      the class is structurally impossible regardless of actor, and the
      runtime now logs tag-vs-built-ID mismatches loudly — the next
      occurrence names itself. ELIMINATED so far (carla, 16:0xZ): user-level journal (empty
      for the window), user systemd timers (only launchpadlib-cache-clean,
      daily at 12:46), user crontab (none). REMAINING trails: dockerd
      journal (needs sudo), the removed container's Created metadata
      (gone with the rm — Alan saw 06:52:50Z before removal, source?),
      any root-level cron/timer.
- [ ] Restart policy + recreate-convention for treadmill-api: crash-looping
      on a missing migration should page/relay loudly, not retry silently
      into a 9-hour gap (op-readiness class: a dead API is a never-fired
      path for every event consumer downstream).
- [ ] `treadmill-local up`'s `cdk synth` step fails on current main
      (surfaced during recovery; AWS-side infra dir; separate issue).
