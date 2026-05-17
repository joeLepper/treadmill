"""Treadmill cron scheduler (ADR-0035).

Three submodules:

  cron    — thin croniter wrapper (next_fire_time, iter_fires)
  policy  — deterministic jitter, quiet-hours, backoff (ported from RAMJAC)
  runner  — SchedulerRunner: 30 s poll loop + startup missed-tick replay
"""
