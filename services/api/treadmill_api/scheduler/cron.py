"""croniter wrapper (ADR-0035).

Thin adapter so the rest of the scheduler imports a stable internal API
rather than reaching for croniter directly. Timezone-aware datetimes are
passed through unchanged; timezone-naive inputs produce timezone-naive
outputs (croniter preserves tzinfo on the seed object).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from croniter import croniter as CronIter


def next_fire_time(cron_expression: str, after: datetime) -> datetime:
    """Return the earliest fire time strictly after ``after``."""
    return CronIter(cron_expression, after).get_next(datetime)


def iter_fires(
    cron_expression: str,
    start: datetime,
    end: datetime,
) -> Iterator[datetime]:
    """Yield every fire time in the half-open interval ``[start, end)``.

    Yields nothing when ``start >= end``.
    """
    if start >= end:
        return
    itr = CronIter(cron_expression, start)
    while True:
        t = itr.get_next(datetime)
        if t >= end:
            break
        yield t
