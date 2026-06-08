---
date: 2026-06-08
trigger: correction
status: captured
---

# Learning: SQLAlchemy create_engine "expected string or URL" error text is not the env var name

## Trigger
During the Plan C GCP sprint, DED logs showed:
`sqlalchemy.exc.ArgumentError: Expected string or URL object, got None`
I reported this as "DATABASE_URL env var is None" to Carla. Carla corrected that DED reads `DED_DATABASE_URL` (proto_message.py:32), not `DATABASE_URL` — SQLAlchemy's error message uses its own generic phrasing for the `url` argument, not the env var name.

## Observation
SQLAlchemy's `create_engine(url)` raises `ArgumentError: Expected string or URL object, got None` when the value passed to it is `None`. The error text says nothing about which env var was read — it only reflects that the value passed to `create_engine` was `None`. The env var lookup (`os.getenv(...)`) happens one or more frames above this in application code.

## Generalization
When diagnosing "expected string or URL object, got None" from SQLAlchemy, the env var name in the error is NOT the variable that is missing. Always grep the service's source for `os.getenv` or `settings.<field>` near `create_engine` to find the actual env var being read.

## Proposed rule
When a SQLAlchemy create_engine error names a missing env var, verify the actual env var name from source (`grep -r "create_engine\|DATABASE_URL" src/`) before filing or relaying a diagnosis.

## Proposed remediation
none — judgment call during chain debug; the grep step takes 10 seconds.
