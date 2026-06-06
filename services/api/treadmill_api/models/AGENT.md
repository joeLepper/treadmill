# SystemStatus — Autoscaler Heartbeat Row

## Overview

`SystemStatus` is a single-row-per-family ORM model that tracks autoscaler state for each worker family. Updated by the autoscaler's heartbeat at the end of each tick (success or failure path). Detectors (task 4) read this row via the `GET /api/v1/system_status/{family}` endpoint to observe spawn history and failure counts.

## Schema

**Table:** `system_status`

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `family` | VARCHAR(64) | NO | — | PK: Worker family identifier (e.g., `"worker-default"`) |
| `worker_count` | INTEGER | NO | 0 | Current count of running workers |
| `last_spawn_at` | TIMESTAMPTZ | YES | NULL | Timestamp of the most recent successful spawn |
| `last_spawn_error` | TEXT | YES | NULL | Truncated error message from the most recent spawn failure |
| `last_consume_at` | TIMESTAMPTZ | YES | NULL | Reserved for task 4 (detector consumes from queue) |
| `consecutive_spawn_failures` | INTEGER | NO | 0 | Count of consecutive spawn failures (reset to 0 on success) |
| `updated_at` | TIMESTAMPTZ | NO | now() | Timestamp of the last heartbeat write |

**Indexes:**
- `ix_system_status_updated_at` — Plain index on `updated_at` for detector queries

## Autoscaler Heartbeat Integration

The autoscaler writes a heartbeat at the END of each `tick()`, regardless of success or failure:

### Success Path (spawn succeeded)
- `worker_count` ← current count
- `last_spawn_at` ← `now()`
- `consecutive_spawn_failures` ← 0
- `last_spawn_error` ← NULL

### Failure Path (spawn failed)
- `worker_count` ← current count
- `last_spawn_at` ← unchanged
- `consecutive_spawn_failures` ← increment by 1
- `last_spawn_error` ← truncated exception message (1000 chars max)

### No Spawn (desired == current)
- All fields unchanged
- Heartbeat still written (updated_at refreshed)

## API Endpoints

**POST /api/v1/system_status/heartbeat** (autoscaler writes)
- Request: `HeartbeatRequest` with family, worker_count, last_spawn_at, last_spawn_error, consecutive_spawn_failures
- Response: `{"status": "ok"}`
- Behavior: Upsert (create if not exists, update if exists)

**GET /api/v1/system_status/{family}** (detectors read)
- Response: `SystemStatusResponse` with all fields
- Status codes: 200 OK, 404 Not Found

## Related

- **Autoscaler:** `tools/local-adapter/treadmill_local/autoscaler.py` — writes heartbeat at end of each tick
- **Migration:** `services/api/alembic/versions/20260605_1900_add_system_status.py` — creates the table
- **Router:** `services/api/treadmill_api/routers/system_status.py` — POST + GET endpoints
- **Tests:** `services/api/tests/test_system_status_heartbeat.py` — unit tests for write/read paths
