- **[#352](https://github.com/joeLepper/treadmill/pull/352) Durable 5-h limit-park sweep (task 2b8fd900)**: `treadmill-limit-park-sweep`
  (non-LLM bash sweeper), `treadmill-limit-park-sweep.service` (oneshot), and
  `treadmill-limit-park-sweep.timer` (`OnCalendar=*-*-* 00/5:00:00`,
  `Persistent=true`). Replaces the CronCreate stopgap (session-only, 7-day
  expiry, alan-dependent) with a persistent systemd timer. On a confirmed park
  the sweep calls `treadmill-limit-park-recover` (event + potential failover)
  then bounces the unit; the launcher's startup poller dismisses the stale
  modal on relaunch. Test: `test_limit_park_sweep.py`.
