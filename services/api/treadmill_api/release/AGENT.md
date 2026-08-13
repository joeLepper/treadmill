# `treadmill_api/release` — ADR-0096 drift detection and no-hot-load falsifier

## Purpose

This package implements the mechanical falsifiers for ADR-0096 (releases from
main only, no hot-loading).  It provides two capabilities:

1. **Drift detection** (`drift.py`): computes a content-hash manifest over a
   set of runtime files and compares a claimed manifest against the live tree.
   The falsifier condition is: a running process whose behaviour changes
   without a restart, or two hosts on the same release running different code
   versions.

2. **Static no-hot-load scan** (`hotload_scan.py`): walks the framework
   source tree looking for banned hot-load patterns (importlib.reload,
   imp.reload, exec applied to source text, sys.modules monkeypatching).  An
   empty result confirms the framework ships no hot-load path.

Both modules are pure: no network, no live process introspection.

The runtime poller that sources a real release version label from a release
artefact and schedules periodic drift checks is **deferred** — it is the next
layer above this package, not part of this task.

## Key surfaces

- **`drift.py`** — `manifest_for(paths, version)`, `detect_drift(claimed, live_paths)`,
  `same_version_different_code(manifest_a, manifest_b)`.  All inputs are
  explicit `Path` arguments so tests can pass temporary trees.
- **`hotload_scan.py`** — `scan_for_hotload(root)`.  Returns `(relative_path, lineno)`
  tuples; empty list means the tree is clean.
- **`__init__.py`** — empty; the package boundary.

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task d5c570c1):
> add `agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md` beside this AGENT.md —
> one entry per file, newest by filename; format in `docs/agent-md-schema.md`.
> Prepending here is the conflict factory that stacks same-day rework cascades
> (every in-flight PR inserts at this same anchor).

## Pitfalls

- `manifest_for` uses `str(path)` as the dict key.  Pass the same style of
  paths (all absolute or all relative) to `manifest_for` and `detect_drift`
  to avoid phantom drift from key mismatches.
- `hotload_scan` excludes any path component named `tests` or `test`.  It
  also excludes itself (`hotload_scan.py`).  The `exec(` pattern is broad;
  if a future legitimate use of `exec` appears in the framework tree, add a
  narrower pattern or an explicit per-file exclusion rather than widening the
  scanner.
- `same_version_different_code` returns `False` when either manifest carries
  `version=None` — a version-unlabelled manifest cannot assert the two-hosts
  falsifier.  The deferred poller is responsible for stamping version labels.

## Navigation

- **Decision:** ADR-0096 (releases from main only, no hot-loading) — the
  policy this package makes falsifiable.
- **Restart mechanism:** ADR-0073 (systemd-per-label substrate) — how a host
  adopts a release.
- **Tests:** `services/api/tests/test_release_drift.py`,
  `services/api/tests/test_no_hotload_paths.py`.
