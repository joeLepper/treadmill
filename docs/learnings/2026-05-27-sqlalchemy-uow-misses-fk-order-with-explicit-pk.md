---
date: 2026-05-27
trigger: incident
status: captured
related: ADR-0057, 2026-05-27-stub-session-tests-hide-fk-violations
---

# Learning: SQLAlchemy's UnitOfWork can't infer FK insert order when the parent PK is passed explicitly

## Trigger

`seed_system_plan_if_empty` (ADR-0057, PR #40) crashed the API on startup
with a `ForeignKeyViolation` even though the Plan and the Event were added
to the same Session in parent-then-child order:

```python
session.add(Plan(id=SYSTEM_PLAN_ID, ...))
session.add(Event(plan_id=SYSTEM_PLAN_ID, ...))
session.commit()
```

SQLAlchemy's UnitOfWork is supposed to topologically sort pending INSERTs
by FK dependency — child INSERTs are reordered to run after parent INSERTs
regardless of `add()` order. So why didn't it?

## Observation

The Plan model declares its PK with a server default:

```python
id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    primary_key=True,
    server_default=text("gen_random_uuid()"),
)
```

When SQLAlchemy builds its dependency graph, it tracks "this child row's FK
points to a *pending* parent row" by linking the child's FK *attribute* to
the parent's *unflushed PK*. That linkage is established when the child is
constructed and references the parent (e.g.
`Event(plan=plan_instance)` — relationship-style), OR when the parent is
detected as "PK comes from a server-side default" and the child carries
the same Python-side identity reference.

We did neither. We passed `plan_id=SYSTEM_PLAN_ID` as a literal UUID value
on the Event, and we *also* passed `id=SYSTEM_PLAN_ID` as a literal UUID
on the Plan — bypassing the `server_default=gen_random_uuid()`. From
SQLAlchemy's perspective these are two unrelated inserts that happen to
share a value. The dependency analyzer doesn't peek at column values to
discover relationships; it only follows declared `relationship()` /
attribute references.

So at commit time, with both rows pending and no inferred ordering, the
Event INSERT was emitted first. Postgres saw `plan_id = '00000000-...'`,
checked `plans.id` for that UUID, didn't find it, raised the FK violation.

## The rule

**When you pass an explicit PK on the parent AND a literal FK value on the
child (instead of using a `relationship()` or the parent's `server_default`),
SQLAlchemy cannot infer the insert order. You must `flush()` between the
two `add()` calls.**

```python
session.add(parent)
session.flush()         # parent INSERT runs now; PK is durable in this txn
session.add(child)      # FK check at next flush/commit sees the parent row
session.commit()
```

The fix shipped in PR #41 was one line — the lesson is in *predicting*
when that line is required, not in writing it.

## How to spot this before it crashes prod

Three flags that indicate UnitOfWork won't infer the order:

1. **The parent declares `server_default` on its PK, but you pass the PK
   explicitly.** You're bypassing the dependency-tracking signal that
   server-default PKs would otherwise produce.
2. **The child references the parent by FK value, not by `relationship()`.**
   Passing `plan_id=some_uuid` is a value-equality, not a Python identity
   reference. SQLA won't link them.
3. **Both parent and child are inserted in the same `session.commit()`
   without an intervening `flush()`.** This is the necessary trigger; the
   first two without this one are latent.

When all three apply, `session.flush()` between the adds is mandatory.

## Codebase scan

Searched for the pattern in `services/api/`:

- `seed/system_plan.py` — fixed in PR #41 (the incident).
- `seed/starters.py` — inserts roles + workflows; workflows reference roles
  by FK. Uses `relationship()`-style construction, so SQLA's dependency
  graph picks up the ordering. Safe.
- `seed/schedules.py` — single-table inserts (Schedule rows, no FK to
  pending parent). Safe.
- `routers/tasks.py::create_task` — explicit `session.add(task);
  await session.flush()` then later inserts that reference task.id. Safe
  (the flush is deliberate, captures task.id for the subsequent
  `persist_and_publish`).
- `routers/plans.py::create_plan` — same flush-after-parent pattern. Safe.

No other instances of the bug in the seed layer. The `routers/` paths all
use explicit `flush()` for an unrelated reason (they need to read the
auto-generated `id` back), which incidentally also satisfies the FK
ordering rule. Lucky structural coincidence.

## Generalization

The bigger lesson: **SQLAlchemy's "smart" auto-ordering is an inference, and
inferences have failure modes.** Any time you bypass the signals the
inference relies on (`relationship()`, server-default PKs, FK columns
populated from a pending parent's identity), you must order INSERTs manually.

For sentinel rows with stable explicit PKs (like the system Plan), this is
the normal case, not the exception. Treat `flush()` between parent + child
adds as load-bearing, not optional.
