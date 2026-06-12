---
date: 2026-06-11
trigger: pattern
status: crystallized-into-rule-unit-restart-reaps-launcher-tree
crystallized_into: tools/cc-channels/systemd/treadmill-channel-reap
related: ADR-0073 (single-instance contract), ADR-0089 (wake filter — the work the restarts were picking up)
---

# Learning: a live orphaned launcher survives fleet sleep and crashloops the unit

## Trigger
On 2026-06-11, four named sessions (coordinator-joelepper-treadmill,
worker-joelepper-treadmill-1, treadmill-carla, treadmill-donna) each had to
be manually recovered from the same failure within hours: their
`treadmill-channel@<label>` systemd unit was `activating auto-restart`
with NRestarts in the thousands (Donna's reached 4119), refusing to start.

## Observation
The blocker each time was a **live** orphaned launcher process from the
pre-sleep session (Carla pid 1230193, Donna pid 1812814) still holding the
ADR-0073 single-instance lock (`~/.cc-channels/<label>/launcher.pid`). The
guard correctly refused to start a second instance — but the first instance
was a zombie running the OLD (pre-wake-filter) channel server, detached from
the unit's supervision. `systemctl stop` had not reaped it. Recovery was
always: stop unit → `kill` the orphan PID (it died on TERM — confirming it
was alive, not a stale file) → rm launcher.pid → reset-failed → start.

Correction to an earlier framing of mine: this is NOT a dead-PID stale lock.
The orphan is a live, supervision-detached process; the lock is doing its job.
(A genuinely distinct case the same day — worker-1 parked at a usage-limit
prompt — is a separate class; do not conflate.)

## Generalization
A ~9h fleet idle (sessions sleeping across the machine's quiet hours) is
enough to detach launcher processes from their systemd cgroup, so a later
unit restart races a surviving orphan it can neither see nor reap. The
single-instance guard then converts one orphan into an unbounded restart
loop. Frequency (4/day) makes this operational toil, not an incident.

## Proposed rule
A `treadmill-channel@` unit restart must reap its entire prior process tree;
no launcher survives a stop/restart.

## Proposed remediation
Investigate two fixes (task registered): (a) the systemd unit's KillMode /
cgroup membership so `stop` tears down launcher + claude + channel-server as
one tree; (b) failing that, the single-instance guard reclaims — on finding a
launcher PID whose owning unit is itself (in auto-restart), kill it and take
over. Prefer (a); (b) is the fallback. Until shipped, the manual sequence
above is the recovery (orchestrators run it for siblings; ADR-0073).

## Notes
The restarts were each picking up the ADR-0089 wake filter — so the toil
was self-inflicted by good work (the filter) meeting an old supervision gap.
