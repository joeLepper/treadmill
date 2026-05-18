---
date: 2026-05-18
trigger: correction
status: captured
related: ADR-0020 (observability), PR #147
---

# Learning: Autoscaler heartbeat reinvented logging when the log file already worked

## Trigger

Joe, on reviewing #147 (autoscaler heartbeat pulse file): *"What is the pulse for? Why are we writing to a file? Did you reinvent logging?"*

Yes. I did.

## Observation

The 2026-05-17 autoscaler-silently-died learning proposed a "file, DB row, or OTel gauge" heartbeat as a liveness signal. I chose file. I implemented it as a sibling artifact (`autoscaler.pulse`) alongside the existing `autoscaler.log`, wrote an `_write_pulse` method that calls `Path.touch()` + `os.utime` every tick, threaded `pulse_path` through the constructor, added an env var for the path, and taught `status` to read the pulse mtime.

The existing `autoscaler.log` file already exists. It is written by Python `logging`. The autoscaler already calls `logger.info(...)` on ticks where work happened. The reason "log mtime as heartbeat" didn't naively work is that the existing tick log is *conditional* — `if t.started > 0 or t.depth > 0:` — so idle ticks (depth=0, started=0) produce no output and the log mtime stays stale during legitimate idle. **That conditional is the actual bug.** Removing it (or adding an unconditional `logger.debug` / `logger.info` per tick) would make the log file itself a sufficient pulse. `status` would read the log mtime. Zero new files, zero new env vars, zero new code surface.

The pulse file isn't wrong — it works. But it's a *parallel persistence mechanism* introduced because I didn't recognize that the existing one was already designed for this use case, just with a stale guard.

## Generalization

When I'm about to introduce a new persistence artifact (file, table, queue, key) to a system, I should first ask: **what's already persisting in this neighborhood, and would extending it cover this case?** The bunkhouse-precedent rule already says this for shapes/schemas; the rule extends to *substrates*. Logging is a substrate. Files-on-disk-with-mtime is a substrate. Don't add a second substrate that does the same job as an existing one with a small modification.

The proxy signal: when my new feature can be replaced by adjusting one conditional or one log level in existing code, that's a strong signal I'm reinventing. The existing code's authors *had a reason* for the substrate they chose; respect that and extend it.

## Proposed rule

Before adding a new persistence file/table/key for an operational signal, demonstrate that the closest existing substrate cannot be extended to cover the case. "Can't be extended" must mean a concrete technical block (schema mismatch, write-cost, durability tier, etc.) — not "I didn't think of it." If the existing substrate would work with a one-line change, use that one-line change.

## Proposed remediation

Follow-up PR: replace the pulse-file mechanism with an unconditional `logger.info("tick: ...", ...)` per autoscaler tick (or `logger.debug` if too noisy at INFO). `status` reads `autoscaler.log` mtime as the liveness signal. Remove `pulse_path`, `_write_pulse`, `AUTOSCALER_PULSE_FILE`, and the `TREADMILL_AUTOSCALER_PULSE_FILE` env-var plumbing.

Net code reduction: ~50 lines removed, ~2 lines added.

## Notes

- The reverse direction (don't collapse substrates that look similar but aren't) is sometimes the right call — e.g. don't mix audit-trail events into application logs. But this isn't that case; both are operator-visible "is this thing alive" signals on the same substrate (mtime of a file on disk).
- Related: [[feedback-bunkhouse-precedent-shapes]] applies to shapes; this learning extends the same principle to substrates.
- The pulse file is live in production as of 2026-05-18 (#147 merged). The follow-up to collapse it back to logging is a small, safe change.
