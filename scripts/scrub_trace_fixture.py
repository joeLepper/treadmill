"""Scrub a captured event-trace fixture before it ships in the public repo.

Reads a gzipped JSONL trace, replaces every occurrence of the medicoder
domain identifiers with a domain-neutral substitute, and writes the
scrubbed fixture to the destination path.

The treadmill repo is public; the medicoder repo identity is not. Any
trace captured from a medicoder plan must be scrubbed before it lands.

Usage:
    python scripts/scrub_trace_fixture.py <input.jsonl.gz> <output.jsonl.gz>
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

# Order matters: replace the longer + more specific token first so we
# don't leave a half-scrubbed orphan like "acme/widget" → "acme/widget"
# after a naive sequential replace on overlapping prefixes.
_SCRUBS: tuple[tuple[str, str], ...] = (
    ("MediCoderHQ/medicoder", "acme/widget"),
    ("MediCoderHQ", "acme"),
    ("medicoder", "widget"),
)


def scrub(line: str) -> str:
    for needle, replacement in _SCRUBS:
        line = line.replace(needle, replacement)
    return line


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write(__doc__ or "")
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with gzip.open(src, "rt") as fin, gzip.open(dst, "wt") as fout:
        for line in fin:
            fout.write(scrub(line))
            written += 1
    print(f"scrubbed {written} lines: {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
