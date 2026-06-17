---
name: Obsidian vault file layout convention
description: Vault files are stored as {slug}/{doc_path}.md where slug is repo-derived; used by ADR-0078, ADR-0054, ADR-0030
type: reference
---

## Obsidian Vault Layout Convention

Obsidian vault files synced with Treadmill follow a deterministic naming convention:

**Path pattern:** `{slug}/{doc_path}.md`

- **`{slug}`** — Normalized repo identifier (e.g., `treadmill`, `ramjac-prod`, `ZEPHYR-zephyr`). Derived from the repo's `RepoConfig` identifier and normalized to lowercase, hyphens, no special chars.
- **`{doc_path}`** — Document path within that repo's docs (e.g., `adrs/0078-bidirectional-obsidian-sync`, `plans/2026-06-05-vault-sync-daemon`, `learnings/2026-06-05-mobile-authoring`).

This convention ensures:
- Deterministic, repeatable path resolution across conform and adapt modes.
- Readable file layout (slugs group files by repo; docs are browsable by type).
- Multi-device consistency (phone, server, local mirror all use the same paths).

**Related ADRs:**
- ADR-0054 — Adapt-mode doc authoring via local mirror (read-side sync).
- ADR-0078 — Bidirectional Obsidian sync via systemd daemon (write-side sync, uses this convention for path normalization).
- ADR-0030 — Federated in-repo agent context (conform-mode authoring).
