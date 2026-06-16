---
date: 2026-06-12
trigger: correction
status: captured
related: plan-2026-05-26-treadmill-dashboard-v1
---

# Learning: A UI never falls back to fake data

## Trigger
During dashboard v2 real-data wiring, I wired each screen to its live
endpoint but added a mock fallback with a visible `live`/`mock` provenance
chip — so a screen whose endpoint was unreachable would render fabricated
data labeled "mock". Joe rejected the entire pattern: "Don't build
mechanisms that fall back to fake data. We don't want a UI that lies to the
user, even if it tells the user that it's lying."

## Observation
We shipped, across ~10 screens, a `liveData ?? mockData` fallback plus a
chip announcing which one was showing. The label did not redeem it: an
operator scanning the dashboard for "what is happening right now" cannot
trust a surface that sometimes shows invented numbers. Separately, where the
live data was thinner than the UI wanted (journey gate-cycles, per-task
cost), I composed-with-gaps in the frontend instead of serving it from the
API.

## Generalization
We tend to reach for mock fallbacks to keep a UI "looking complete" when
data is missing, and to compose around API gaps in the client rather than
extend the API. Both trade trust for the appearance of completeness. A
status-of-the-system surface must be honest by construction: it shows real
data, or it shows that it is loading / empty / errored — never a fabrication.
When the data the UI needs does not exist yet, the fix is a new API endpoint,
not a client-side workaround.

## Proposed rule
A data-display UI must render exactly one of: live data, loading, empty, or
error. It must never substitute mock/fabricated data, labeled or not. Missing
data the UI needs is an API gap to close, not a frontend gap to paper over.

## Proposed remediation
Code-review check (LLM-judge): flag any component that selects between a
fetch result and a hardcoded/imported fixture for render. Grep heuristic:
`?? mock`, `|| mock`, `liveData ? … : mock…` in page/component files.

## Notes
The honest-provenance instinct (the chip) was right in spirit — the user
should know data freshness — but freshness is conveyed by the
loading/stale/error affordance, not by swapping in a fake. Keep the
ConnectionAffordance; drop the fallback.
