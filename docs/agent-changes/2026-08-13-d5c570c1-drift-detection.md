- **New `services/api/treadmill_api/release/` package (task d5c570c1, ADR-0096)**: adds the
  ADR-0096 mechanical falsifiers — drift detection (`drift.py`: `manifest_for`,
  `detect_drift`, `same_version_different_code`) and a static no-hot-load scanner
  (`hotload_scan.py`: `scan_for_hotload`).  Both are pure modules (no network, no
  live-process introspection).  Tests in `services/api/tests/test_release_drift.py`
  and `test_no_hotload_paths.py`.  The runtime poller that sources version labels
  from real release artefacts is deferred.  PR: to be added on merge.
