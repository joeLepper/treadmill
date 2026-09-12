# ADR-0103: Shared agent skills have one versioned canonical source, linked into every harness

- **Status:** accepted (2026-09-11; cross-model review by Fran + sibling verified co-sign by treadmill-ernie)
- **Date:** 2026-09-11
- **Related:** ADR-0102 (Codex sibling), ADR-0006 (rules schema), ADR-0008 (learning capture)

## Context

The fleet now runs two agent harnesses: Claude Code (the siblings) and Codex (Fran, ADR-0102). Both use "skills" — `SKILL.md` files (YAML frontmatter + markdown body, auto-selected by `description`). Claude Code loads them from `.claude/skills/` (and `~/.claude/skills/`); Codex loads them from `~/.codex/skills/`.

We want the same authoring/review discipline (`decide`, `adversarial-review`, `learning`, `rule`, `curate-pr`) to govern every agent. The first attempt copied the skills into Codex's directory with `cp -r`. That is a snapshot: an update to one copy never reaches the other, so the copies **diverge silently** — the exact failure the operator flagged ("how can we be confident an update reaches all agents?").

We verified the compatibility question with Fran: Codex **accepts the Claude Code frontmatter as-is** — `name` + `description` suffice, `metadata.short-description` is not required (Codex skill docs). So a shared file needs no per-harness transform.

## Decision

We decided that a shared agent skill has **exactly one versioned canonical source at a single deployed path**, and every harness resolves skills from that path rather than from a per-worktree copy. Concretely:

- The canonical source is a **single deployed checkout pinned to `main`** at a fixed fleet path (the **canonical skills checkout**), holding the shared skills under `.claude/skills/<name>/`. It is advanced by `git fetch origin main && git reset --hard origin/main` **on merge to main** — a deploy step (the checkout is detached and read-only, so a `git pull` does not apply) — never by any agent's own working tree. (This is the fix to the naive "the repo" framing: the fleet has many worktrees on many branches; a bare `.claude/skills` path resolves to *that worktree's* revision, so two agents diverge — verified: two live worktrees held different `decide/SKILL.md` hashes.) User-level shared skills in `~/.claude/skills/` are consolidated into the repo so every shared skill is versioned and reviewable.
- Each harness **resolves skills from the canonical checkout**: a non-Claude-Code harness links each skill (`~/.codex/skills/<name>/` → the canonical skill, linking the **whole skill's resources** — `SKILL.md` plus any `scripts/`, assets, and relative references — not `SKILL.md` alone); Claude-Code agents resolve them via **user-level symlinks** — `~/.claude/skills/<name>` → the canonical checkout's skill dir — which **take precedence over any worktree's project-level `.claude/skills`** (Claude Code resolves skills Personal `~/.claude/skills` **over** Project `./.claude/skills`, and follows symlinks), so a per-worktree copy never shadows canonical and **no live worktree is modified**. A running session picks up a change on its next skill re-index. A per-worktree `.claude/skills` is an editing copy, never the runtime source.
- **Updates flow through PR only.** A skill changes by editing it in a worktree → PR → merge to main → the deploy step advances the canonical checkout → every agent resolves the new revision (after a loader re-index where the harness caches). No agent edits the canonical checkout in place, and no agent runs from a private per-worktree copy.

## Alternatives considered

- **Incumbent: `cp -r` copies (what we did first).** *Why insufficient:* copies diverge; an update to one is invisible to the others; there is no propagation and no single source of truth.
- **Generated sync on merge** — a build step that transforms each canonical skill into a per-harness copy. *Why not primary:* Codex accepts the Claude Code frontmatter unchanged (verified), so no transform is needed; a symlink is simpler and has no build step to run or forget. Kept as the **fallback** for a future harness that requires incompatible frontmatter — it preserves single-source (generated, never hand-edited).
- **Independent per-harness skills.** *Why rejected:* defeats the goal — the fleet would drift into different review/authoring standards per harness.

## Consequences

### Good
- One source of truth. **Propagation of the file is by construction** once the canonical checkout is advanced — every agent resolves the same canonical path — but two triggers must be **automated, not manual**, or the "no sync to remember" claim is false: (1) the on-merge deploy step (`git fetch origin main && git reset --hard origin/main` on the canonical checkout) must run from a merge hook / CI / path-watch, not by hand; (2) a running agent picks up the change only on its next skill **re-index** (§ re-index follow-up). With both automated, no human runs a sync; until then, this is a follow-up, not a guarantee.
- Skill changes are reviewed like any code (PR), and reach every agent on merge.
- Cross-harness parity: Fran reviews and authors under the same discipline as the siblings.

### Bad / trade-offs
- Symlinks are per-host setup: provisioning a Codex agent on a new host must create the links (a small, scriptable step).
- A harness may cache its skill registry: Fran observed the five skills listed (loaded) earlier in a session, then absent from a later registry snapshot — so a running agent may need a **session restart to re-index** newly-linked skills. Discovery-refresh behavior is a follow-up.

### Risks
- An agent edits the symlink target directly, bypassing PR. Mitigated by the PR-only rule and by the canonical file living in the repo, where worktree/merge discipline (only humans merge to main) already applies.
- **Falsifier:** two agents resolve **different content** for the same skill name (a private copy exists somewhere instead of a link to canonical), OR a skill change merged to main is not reflected in a Codex agent after its session re-indexes.

## Follow-ups

- Consolidate the user-level skills (`adversarial-review`, `curate-pr`) into the repo canonical dir; today they link into unversioned `~/.claude/skills/`.
- Determine Codex's skill re-index trigger (does a session restart reliably re-discover linked skills?) and script the per-host link setup for a provisioned Codex agent.
- Decide the shared set (the harness-agnostic skills) vs Claude-Code-only skills (`cc-relay`, `exec-in-charge`, `plan`, `release`), which stay unlinked.

## References

- Verification with Fran (2026-09-11): Codex accepts `name`/`description` frontmatter; `metadata.short-description` not required. Codex skill docs.
- ADR-0102 (Codex sibling), ADR-0006 (rules), ADR-0008 (learning capture).
