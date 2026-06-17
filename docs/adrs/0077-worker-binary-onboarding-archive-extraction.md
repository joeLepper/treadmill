# ADR-0077 — Worker binary onboarding supports archive extraction

- **Status:** accepted
- **Date:** 2026-06-05
- **Related:** ADR-0059 (per-repo worker deps + binary materialization)

## Context

ADR-0059 added a curated `BinarySpec` list per repo. The worker fetches
`download_url`, verifies `sha256_checksum`, writes the bytes verbatim to
`target_path`, and `chmod 0o755`s the result. That contract works for
single-binary tools that ship as one statically-linked file (e.g. cosign,
hadolint, sccache); it does not work for tools that ship as a tarball
containing a directory of sibling binaries.

The first real-world fault surfaced on Plan A (GCP substrate, RAMJAC/ramjac,
2026-06-05). The bootstrap task's `wf-feedback.action` check ran
`pulumi preview --stack <stack>`. The worker's overlay had no `pulumi`
binary; the check failed with `/bin/sh: 2: pulumi: not found`. The
author had no diff to amend (the check failure was environmental, not
content-driven), so wf-feedback handed off to wf-architecture-resolve,
which handed back to wf-feedback — the dead-end loop pattern from
`docs/learnings/2026-06-04-architect-feedback-deadend-loops.md` (and
the operator memory entry of the same shape).

Pulumi ships as `pulumi-vX.Y.Z-linux-x64.tar.gz` containing
`pulumi/pulumi`, `pulumi/pulumi-language-nodejs`,
`pulumi/pulumi-language-python`, and ~22 other sibling plugin binaries.
At runtime the main `pulumi` binary execs its language plugins via
`$PATH` lookup. Pointing the existing `--binary` flag at the tarball URL
would write the gzipped tar payload itself to
`/var/treadmill/repo-bin/pulumi` — a sha256-clean file, but a tarball
rather than an executable.

The same shape will hit any tool with a sibling-plugin model: gcloud
(language-runtime plugins), terraform with provider plugins, kubectl
with auth plugins, the Java JDK, anything packaged as `dist/` +
`<main>` + helpers. Plan A's downstream tasks need gcloud alongside
pulumi; Plan C will need more.

The current contract is correctly minimal — single URL, single sha256,
single target — but it boxes us out of the most common tooling
distribution shape on Linux. The fix that costs least to maintain is to
teach the materializer to extract archive payloads rather than to
republish every tarball as a single-binary wrapper.

## Decision

Extend `_install_binaries` in `workers/agent/treadmill_agent/repo_deps.py`
to detect archive payloads by URL extension and extract them into the
overlay, preserving the existing single-binary code path unchanged.

### (1) Auto-detect archive format from `download_url`

Sniff `download_url` against a fixed extension table:

- `.tar.gz`, `.tgz` → gzipped tar
- `.tar.bz2`, `.tbz2` → bzip2 tar
- `.tar.xz`, `.txz` → xz tar
- `.zip` → zip
- anything else → raw bytes (existing behavior)

URL-extension detection (not Content-Type sniffing, not magic-byte
probe of the downloaded payload) keeps the dispatch deterministic and
makes the operator's intent visible in the `--binary` spec itself.

### (2) Verify sha256 against raw payload BEFORE extraction

The downloaded bytes (the archive itself, byte-for-byte) are checksummed
against `spec.sha256_checksum`. No checksum semantics change. An
archive whose sha256 matches but whose contents fail to extract is
treated as a materialization error (`stage='binary'`), same shape as a
checksum mismatch.

### (3) Reinterpret `target_path` for archives as a directory

When the URL is an archive, the validated `target_path` is the
**extraction directory** rather than the final binary path. The pinned
`/var/treadmill/repo-bin/` prefix invariant (ADR-0059) is preserved —
both the raw and archive paths still live under that prefix. The Pydantic
model is unchanged; the interpretation is per-format inside the worker.

### (4) Strip a single top-level directory if present

After extraction, if the staging area contains exactly one entry and
that entry is itself a directory, hoist its contents up one level (the
`tar --strip-components=1` semantic). This handles the common case
where archive authors wrap everything in a `<projectname>-<version>/`
or `<projectname>/` directory. Multiple top-level entries are left as
extracted.

Strip is applied at most one level. Archives that wrap content two
levels deep are left as-is — the operator can post-process via a
follow-up if needed, but auto-stripping more aggressively risks
collapsing legitimate structure.

### (5) `chmod 0o755` every regular file under the extraction root

Tarballs preserve mode bits, but zip does not, and some tarballs ship
mode `0o644` even for executables. Brute-forcing `0o755` on every
regular file is safe inside the per-spec extraction dir (no other
specs share that subtree) and avoids per-tool special cases.

### (6) Augment overlay `PATH` with bin_path subdirectories

`RepoOverlay.env_overrides()` currently adds only `bin_path` itself to
`PATH`. For raw binaries, that's correct — they live as
`/var/treadmill/repo-bin/<name>`. For an archive whose extraction
landed at `/var/treadmill/repo-bin/pulumi/pulumi`, the binary isn't on
the top-level path entry, and pulumi's sibling-plugin lookup wouldn't
find `pulumi-language-nodejs` even if the user did invoke the binary
via its nested absolute path.

Solution: when `bin_path` is set, the overlay also adds every
immediate child directory of `bin_path` to `PATH`. A raw binary at
`bin_path/cosign` continues to resolve via the top-level entry; an
extracted tool at `bin_path/pulumi/pulumi` resolves because
`bin_path/pulumi/` is on `PATH` and its siblings are co-located.

Only one level deep. The overlay does not recursively walk the bin
tree — operators that need deeper structure can use a raw single-binary
wrapper.

## Consequences

**Positive:**

- Standard archive-distributed tools (pulumi, gcloud SDK, terraform,
  kubectl when shipped as tar, the JDK) can be onboarded with the
  existing `treadmill onboarding update --binary` flow — no per-tool
  wrapper republishing.
- The dead-end loop trigger on Plan A's bootstrap task is removed at
  the root: the check script can call `pulumi` directly and the
  materialized overlay resolves it.
- The sha256 contract is unchanged. Operators continue to pin the
  exact bytes they trust; the worker still refuses to materialize a
  payload that doesn't match.

**Negative:**

- Worker code now does archive extraction. `tarfile` and `zipfile` are
  stdlib; no new package dependency, but the worker is doing more work
  per binary install than before. Mitigated by the existing
  `.deps-hash` cache: extraction happens on cache miss only.
- Archives can contain symlinks, hardlinks, or path-traversal entries
  (`../../etc/passwd`). The implementation must refuse entries whose
  resolved path escapes the extraction root (`tarfile.data_filter` /
  manual zip member-name check). This is enforced in code; the
  failure mode is `WorkerDepsMaterializationError(stage='binary')`.

**Neutral:**

- `BinarySpec` schema is unchanged. The interpretation of `target_path`
  differs per format (file path for raw, directory path for archive)
  but the validation (`/var/treadmill/repo-bin/` prefix, non-empty) is
  the same. A future ADR can add an explicit `archive_format` override
  field if URL-extension detection ever proves insufficient.

## Sequence (high level — implementation lives in the PR)

1. **Archive detection helper** in `repo_deps.py` —
   `_detect_archive_kind(url: str) -> ArchiveKind | None`.
2. **Extraction helper** — `_extract_archive(payload: bytes, kind:
   ArchiveKind, dest: Path)` using stdlib `tarfile` / `zipfile` with
   member-name traversal guard.
3. **Strip-components-1 helper** — `_strip_single_top_dir(dest: Path)`
   applied post-extract.
4. **`_install_binaries` dispatch** — raw vs archive branch off
   `_detect_archive_kind`; sha256 check stays in the common prefix.
5. **`RepoOverlay.env_overrides` extension** — when `bin_path` is set,
   append `[p for p in bin_path.iterdir() if p.is_dir()]` to the
   `PATH` parts list.
6. **Unit tests** in `workers/agent/tests/test_repo_deps.py` covering:
   raw binary (regression), tar.gz with single top-level dir
   (strip-components case), tar.gz without wrapper (multi-top case),
   zip extraction, sha256 mismatch on archive payload, path-traversal
   refusal, `env_overrides` PATH includes nested dirs.

## Alternatives considered

**Alternative A: Re-host a single-binary wrapper per tool.**

Rejected because: the wrapper has to bundle the tarball contents
inline (shar, makeself, a small Go shim that unpacks on first run) and
be republished + re-checksummed per upstream release. Pulumi cuts a
release roughly weekly; gcloud daily. The republishing pipeline
becomes its own maintenance burden, and the operator-curated
`download_url` no longer points at the upstream's signed artifact — it
points at our re-hosted copy, which is exactly the supply-chain
indirection ADR-0059 was written to avoid.

**Alternative B: Add explicit `archive_format` field to `BinarySpec`.**

Rejected for v1 because: URL extension is canonical for the formats we
care about (no real-world `.tar.gz` distributed without that suffix);
adding a schema field forces an alembic migration, a CLI flag, and
documentation updates for a signal that's already in the URL. Can be
added later as an opt-in override if some operator hits an archive
whose URL doesn't carry a recognizable extension.

**Alternative C: Skip and require operators to bake tooling into the
worker image.**

Rejected because: it defeats the per-repo onboarding contract.
ADR-0059 explicitly chose curated-per-repo over baked-into-image so
that tool versions can diverge per repo and operators can bump pulumi
on one repo without rebuilding the worker image and impacting others.
Baking tools back into the image inverts that decision.

**Alternative D: Auto-strip recursively (collapse all single-dir
wrappers, not just one level).**

Rejected because: recursive collapse risks flattening legitimate
structure. A tarball laid out as `<name>/<version>/bin/<tool>` with
that exact intent would be collapsed to `<tool>` and lose the
`bin/` separation, which some tools rely on for relative path
resolution (`../share/`, `../lib/`). One level of strip is the
common-case sweet spot and matches `tar --strip-components=1`'s
default semantic.
