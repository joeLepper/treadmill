# ADR-0044: Datetime-keyed Alembic migration revision IDs

- **Status:** proposed
- **Date:** 2026-05-17
- **Related:** ADR-0011 (data model), incident captured in fix PR #139 (scheduler-migration-collision)

## Context

We have numbered Alembic revisions sequentially from `0001` since the project began. That convention worked while a single contributor authored migrations serially. As Treadmill's agentic loop began authoring migrations concurrently across parallel PRs, the convention started producing collisions:

On 2026-05-16, PR #124 (`0017_schedules.py`) and PR #121 (`0017_task_status_surface_decision_fail.py`) both claimed `revision="0017"`, `down_revision="0016"`. Each PR passed its own author-side validation independently — `alembic upgrade head` against a fresh DB succeeds for either migration in isolation. Both merged within hours of each other. Alembic warned *"Revision 0017 is present more than once"* and silently picked one, leaving the schedules migration orphaned. The entire ADR-0035 scheduler plan (six PRs of code: #124, #127, #128, #130, #131, #132) sat non-functional in main because the table it depended on was never created.

Author-side validation cannot detect this class of failure — the collision only manifests when both migrations exist in the same branch state. We will hit it again under the same conventions.

## Decision

New Alembic migrations adopt a datetime-based revision ID and filename, format `YYYYMMDD_HHMM`. Example: a migration authored at 21:03 local time on 2026-05-17 has `revision="20260517_2103"` and lives at `services/api/alembic/versions/20260517_2103_schedules.py`. The `down_revision` field continues to reference the previous head (sequential or datetime, whichever it was). The chain stays linear; only the ID encoding changes.

Existing migrations `0001` through `0017` keep their current IDs. Renumbering them would require updating `alembic_version` in every deployed DB and is not worth the disruption. The chain becomes a hybrid: `0001 → 0002 → … → 0017 → 20260517_2103_schedules → 20260518_NNNN_<next> → …`. PR #139 is the first migration to use the new format, serving as the proof-of-concept.

## Alternatives considered

- **Status quo (sequential numbering)** — Rejected: agentic concurrency makes collisions a structural likelihood, not a fluke. The 0017 incident cost ~24h of system non-functionality before discovery; future collisions will be worse as more agents author migrations in parallel.
- **Alembic's default hash IDs (e.g. `68593b5775c1`)** — Rejected: opaque to skim. Operators routinely read migration filenames to understand DB state evolution; a hash strips the chronological cue. Datetime preserves chronology and stays unique by construction.
- **Renumber existing migrations to the new format** — Rejected: would invalidate `alembic_version` in every deployed DB. Operator burden + migration risk outweighs consistency benefit. Hybrid chain is acceptable.
- **Add a post-merge CI gate that runs `alembic heads` and fails on >1 line** — Considered, not rejected: still worth doing, since the new format is unique-by-construction but operator-error renames (e.g., copy-pasting an old revision ID) would slip past. This is a complementary defense, not a substitute. Tracked as a follow-up.

## Consequences

### Good
- Collision rate drops to ~0 by construction. Two migrations authored within the same minute by different agents is still possible but vastly less likely than two agents both incrementing from `0016`.
- Filenames remain human-readable and chronological; operators can `ls alembic/versions/` and see the timeline.
- New format requires zero changes to existing tooling — Alembic accepts any string as a revision ID.

### Bad / trade-offs
- Filenames are longer (15-char prefix vs. 4).
- The chain visually mixes two formats during the transition period (months, likely years). Readers must understand the chain is linear regardless of ID style.
- Manual chain inspection (e.g., `git log alembic/versions/`) is slightly harder when sorting by ID alphabetically — datetime IDs sort correctly, but mixing with `0017` puts the sequential ones first lexically.

### Risks
- A contributor or agent unfamiliar with this ADR copy-pastes an existing sequential ID, producing a new collision. Mitigation: the follow-up CI gate (mentioned above) catches this.
- Same-minute collisions remain theoretically possible. Mitigation: add seconds to the format (`YYYYMMDD_HHMMSS`) if we ever observe one. Reserved for if/when.

## References

- Incident: PR #139 — Fix ADR-0035 scheduler: rename schedules migration 0017 → 0018
- Convention precedent: Django migrations (`0001_initial.py`), Flask-Migrate (configurable), and Rails (`YYYYMMDDHHMMSS_create_table.rb`) — the Rails convention is the closest analogue and what this ADR adopts.
