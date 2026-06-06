"""Tests for the daemon's core primitives (ADR-0078 §1-5).

Coverage:

  * ``Normalizer.normalize`` collapses CRLF/CR → LF, trims trailing
    whitespace, and enforces exactly one trailing newline.
  * ``Normalizer.sha256_hex`` is stable across the normalization
    equivalences — content that differs only in line endings or
    trailing whitespace hashes identically.
  * ``Sidecar`` round-trips entries through atomic write/read.
  * ``Sidecar.load`` tolerates a missing file (returns empty) and a
    malformed JSON (logs + returns empty).
  * ``device_id`` is stable: a second call returns the same value.
  * ``GateContext`` and ``GateResult`` construct cleanly + helpers
    return the right decisions.
  * ``WatchLoop._scan_once`` baseline → no dispatch on first scan,
    dispatches on subsequent scans for files that moved forward.

Pure unit tests; no inotify, no daemon, no real filesystem watches.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from treadmill_local.obsidian_sync import (
    GateContext,
    GateResult,
    Normalizer,
    Sidecar,
    SidecarEntry,
    WatchLoop,
    device_id,
)


# ── Normalizer ────────────────────────────────────────────────────────────


def test_normalizer_collapses_crlf_to_lf() -> None:
    assert Normalizer.normalize("a\r\nb\r\nc\r\n") == "a\nb\nc\n"


def test_normalizer_collapses_bare_cr_to_lf() -> None:
    assert Normalizer.normalize("a\rb\rc") == "a\nb\nc\n"


def test_normalizer_strips_trailing_whitespace_per_line() -> None:
    assert Normalizer.normalize("hello   \nworld\t\t\n") == "hello\nworld\n"


def test_normalizer_enforces_single_trailing_newline_when_missing() -> None:
    assert Normalizer.normalize("no newline") == "no newline\n"


def test_normalizer_collapses_multiple_trailing_newlines_to_one() -> None:
    assert Normalizer.normalize("text\n\n\n\n") == "text\n"


def test_normalizer_empty_input_returns_single_newline() -> None:
    # Edge case: empty content normalizes to exactly one newline.
    assert Normalizer.normalize("") == "\n"


def test_normalizer_sha256_is_equal_across_line_ending_variants() -> None:
    crlf = "alpha\r\nbeta\r\n"
    lf = "alpha\nbeta\n"
    mixed = "alpha   \r\nbeta\n\n"
    h_crlf = Normalizer.sha256_hex(crlf)
    h_lf = Normalizer.sha256_hex(lf)
    h_mixed = Normalizer.sha256_hex(mixed)
    assert h_crlf == h_lf == h_mixed


def test_normalizer_sha256_differs_for_genuinely_different_content() -> None:
    assert Normalizer.sha256_hex("hello\n") != Normalizer.sha256_hex("world\n")


# ── Sidecar ──────────────────────────────────────────────────────────────


def test_sidecar_round_trips_entry(tmp_path: Path) -> None:
    sidecar = Sidecar(tmp_path / ".sync-state.json")
    entry = SidecarEntry(
        sha256="abc123", parent_hash_at_push="def456", pushed_at=1700000000.0,
    )
    sidecar.write({"treadmill/plans/foo.md": entry})

    loaded = sidecar.load()
    assert "treadmill/plans/foo.md" in loaded
    got = loaded["treadmill/plans/foo.md"]
    assert got.sha256 == "abc123"
    assert got.parent_hash_at_push == "def456"
    assert got.pushed_at == 1700000000.0


def test_sidecar_load_returns_empty_when_missing(tmp_path: Path) -> None:
    sidecar = Sidecar(tmp_path / "nope.json")
    assert sidecar.load() == {}


def test_sidecar_load_returns_empty_when_json_malformed(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", "utf-8")
    sidecar = Sidecar(path)
    assert sidecar.load() == {}


def test_sidecar_load_skips_entries_with_missing_fields(tmp_path: Path) -> None:
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps({
            "good/plans/a.md": {
                "sha256": "x", "parent_hash_at_push": "y", "pushed_at": 1.0,
            },
            "bad/plans/b.md": {
                "sha256": "x",  # missing parent_hash_at_push + pushed_at
            },
        }),
        "utf-8",
    )
    sidecar = Sidecar(path)
    loaded = sidecar.load()
    assert "good/plans/a.md" in loaded
    assert "bad/plans/b.md" not in loaded


def test_sidecar_update_entry_merges_with_existing(tmp_path: Path) -> None:
    sidecar = Sidecar(tmp_path / ".sync-state.json")
    sidecar.write({
        "a.md": SidecarEntry("h1", "p1", 100.0),
        "b.md": SidecarEntry("h2", "p2", 200.0),
    })
    sidecar.update_entry("a.md", SidecarEntry("h1_new", "p1_new", 300.0))

    loaded = sidecar.load()
    assert loaded["a.md"].sha256 == "h1_new"
    assert loaded["a.md"].pushed_at == 300.0
    # Unrelated entry preserved.
    assert loaded["b.md"].sha256 == "h2"


# ── device_id ────────────────────────────────────────────────────────────


def test_device_id_stable_across_calls(tmp_path: Path) -> None:
    first = device_id(tmp_path)
    second = device_id(tmp_path)
    assert first == second


def test_device_id_persisted_to_file(tmp_path: Path) -> None:
    did = device_id(tmp_path)
    path = tmp_path / "device-id"
    assert path.exists()
    assert path.read_text("utf-8").strip() == did


def test_device_id_contains_hostname(tmp_path: Path) -> None:
    import socket
    did = device_id(tmp_path)
    assert did.startswith(socket.gethostname() + "-")


# ── GateContext / GateResult ─────────────────────────────────────────────


def test_gate_context_constructs_with_required_fields(tmp_path: Path) -> None:
    ctx = GateContext(
        vault_path=tmp_path / "vault/treadmill/plans/foo.md",
        source_kind="conform",
        source_repo="joeLepper/treadmill",
        file_relpath="plans/foo.md",
        vault_content="hello\n",
        source_content="hello\n",
        source_hash="abc",
        sidecar_entry=None,
    )
    assert ctx.source_kind == "conform"
    assert ctx.extras == {}


def test_gate_result_passed_helper() -> None:
    r = GateResult.passed()
    assert r.decision == "pass"
    assert r.payload == {}


def test_gate_result_held_helper_carries_payload() -> None:
    r = GateResult.held("filename_invalid", offending_name="Untitled.md")
    assert r.decision == "hold"
    assert r.reason == "filename_invalid"
    assert r.payload == {"offending_name": "Untitled.md"}


def test_gate_result_skipped_helper() -> None:
    r = GateResult.skipped("not an ADR")
    assert r.decision == "skip"


# ── WatchLoop._scan_once ─────────────────────────────────────────────────


def test_watchloop_first_scan_establishes_baseline_no_dispatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    (root / "plans").mkdir(parents=True)
    (root / "plans" / "a.md").write_text("a", "utf-8")
    (root / "plans" / "b.md").write_text("b", "utf-8")

    handler = MagicMock()
    loop = WatchLoop([root], handler)
    loop._scan_once()

    # Baseline established silently.
    handler.assert_not_called()


def test_watchloop_second_scan_dispatches_for_modified_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    (root / "plans").mkdir(parents=True)
    f = root / "plans" / "a.md"
    f.write_text("original", "utf-8")

    handler = MagicMock()
    loop = WatchLoop([root], handler)
    loop._scan_once()  # baseline

    # Bump mtime forward and write new content.
    time.sleep(0.01)
    f.write_text("modified", "utf-8")
    new_time = time.time() + 1.0
    import os
    os.utime(f, (new_time, new_time))

    loop._scan_once()
    handler.assert_called_once_with(f)


def test_watchloop_does_not_dispatch_non_md_files(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "notes.txt").write_text("not markdown", "utf-8")
    (root / "config.json").write_text("{}", "utf-8")

    handler = MagicMock()
    loop = WatchLoop([root], handler)
    loop._scan_once()  # baseline
    # Touch the non-md files.
    import os
    new_time = time.time() + 1.0
    os.utime(root / "notes.txt", (new_time, new_time))
    os.utime(root / "config.json", (new_time, new_time))
    loop._scan_once()

    handler.assert_not_called()


def test_watchloop_handles_missing_root_gracefully(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    handler = MagicMock()
    loop = WatchLoop([missing], handler)
    # Should not raise.
    loop._scan_once()
    loop._scan_once()
    handler.assert_not_called()


def test_watchloop_handler_exception_does_not_break_loop(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    a = root / "a.md"
    b = root / "b.md"
    a.write_text("a", "utf-8")
    b.write_text("b", "utf-8")

    calls: list[Path] = []

    def raising_handler(path: Path) -> None:
        calls.append(path)
        if path.name == "a.md":
            raise RuntimeError("boom")

    loop = WatchLoop([root], raising_handler)
    loop._scan_once()  # baseline

    # Modify both.
    import os
    new_time = time.time() + 1.0
    a.write_text("a2", "utf-8")
    b.write_text("b2", "utf-8")
    os.utime(a, (new_time, new_time))
    os.utime(b, (new_time, new_time))

    loop._scan_once()
    # Both should have been called even though a.md raised.
    paths_called = {c.name for c in calls}
    assert paths_called == {"a.md", "b.md"}


def test_watchloop_stop_signals_run_to_exit() -> None:
    handler = MagicMock()
    loop = WatchLoop([], handler, poll_interval_seconds=0.01)
    loop.stop()
    # run() should exit promptly because _stop is set.
    loop.run()
