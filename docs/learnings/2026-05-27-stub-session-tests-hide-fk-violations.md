---
date: 2026-05-27
trigger: incident
status: captured
related: ADR-0057, 2026-05-22-docker-restart-reuses-old-image-silent-noop-deploy
---

# Learning: Stub-session unit tests can't see FK constraints

## Trigger

PR #40 (ADR-0057 — synthetic-task dispatch) shipped with 4 green unit tests
covering `seed_system_plan_if_empty`, including one named
`test_seed_system_plan_if_empty_fresh_db_inserts_plan_and_activated_event`
that asserted both rows were inserted. The deploy-watcher autodeployed and
the API immediately crashed with:

```
sqlalchemy.exc.IntegrityError: (psycopg.errors.ForeignKeyViolation)
insert or update on table "events" violates foreign key constraint
"events_plan_id_fkey"
DETAIL: Key (plan_id)=(00000000-0000-0000-0000-000000000001)
        is not present in table "plans".
```

The unit tests were green; the seed crashed on the very first run against a
real Postgres.

## Observation

The seed code added two rows in one transaction:

```python
session.add(Plan(id=SYSTEM_PLAN_ID, ...))   # parent
session.add(Event(plan_id=SYSTEM_PLAN_ID, ...))   # child via FK
session.commit()
```

The unit test used a stub session whose `add` only records the entities to a
list and whose `commit` flips a boolean. The stub doesn't actually run SQL,
so it doesn't see — can't see — any FK constraint. Insert order is invisible
to it; FK enforcement is invisible to it; deferred-constraint semantics are
invisible to it.

That's a category of bug the test was structurally incapable of catching, no
matter how many assertions it carried.

## The rule

**When a seed/insert spans an FK relationship, the unit test must pin
*insert order* — not just "both rows were added."**

Adding to a stub-session list is not the same as making the row visible to a
later FK check. A test that asserts `len(added) == 2` passes whether you
insert parent-then-child or child-then-parent. Postgres rejects one of those;
your test rejects neither.

The regression guard PR #41 shipped:

```python
def test_seed_system_plan_if_empty_flushes_plan_before_event_insert():
    session = _StubSession(existing=None)
    seed_system_plan_if_empty(session)
    assert session.call_log == [
        "add:plan", "flush", "add:event", "commit",
    ], f"insert order wrong: {session.call_log}"
```

The stub session now tracks an interleaved `call_log` of add/flush/commit so
the test asserts the exact sequence the FK constraint requires.

## Generalization

Three classes of bug that stub-session tests cannot catch:

1. **FK ordering** (this incident) — child INSERT before parent INSERT.
2. **NOT NULL violations on default-bearing columns** — stub doesn't apply
   server defaults; passing through Python `None` looks fine, Postgres
   rejects.
3. **Uniqueness violations under concurrent insert** — stub has no notion
   of `UNIQUE` constraints.

For each, the test pattern is the same: don't assert "row was added", assert
the *protocol* the underlying DB enforces.

## When to reach for what

- **Stub session** — verifying that *your code* makes the right method calls
  in the right order (control-flow, not data-flow).
- **In-memory Postgres / testcontainers** — verifying that the *DB* accepts
  what your code is about to send it. For any code that spans an FK,
  uniqueness, NOT NULL, or check constraint, this is the only honest test.
- **Pinning the call log in the stub** — middle ground; cheaper than
  spinning up Postgres and catches the structural-order bugs that 100% of
  FK violations reduce to.

The `seed/` modules in this codebase have ~5 callers each that insert across
FKs (starters, schedules, system_plan). They should all carry call-log-order
tests, even when an integration test also exists — the unit test is the one
that runs in the inner loop and the one that catches the bug in pre-merge CI.
