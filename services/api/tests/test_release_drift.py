"""Tests for treadmill_api.release.drift — ADR-0096 drift detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from treadmill_api.release.drift import detect_drift, manifest_for, same_version_different_code


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_matching_manifest_reports_no_drift(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    b = _write(tmp_path, "b.py", "y = 2\n")
    paths = [a, b]
    claimed = manifest_for(paths)
    report = detect_drift(claimed, paths)
    assert not report.in_drift
    assert report.changed == []
    assert report.missing == []
    assert report.added == []


def test_changed_file_reports_drift(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    b = _write(tmp_path, "b.py", "y = 2\n")
    paths = [a, b]
    claimed = manifest_for(paths)

    b.write_text("y = 99\n", encoding="utf-8")

    report = detect_drift(claimed, paths)
    assert report.in_drift
    assert str(b) in report.changed
    assert str(a) not in report.changed


def test_missing_file_reports_drift(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    b = _write(tmp_path, "b.py", "y = 2\n")
    claimed = manifest_for([a, b])

    report = detect_drift(claimed, [a])
    assert report.in_drift
    assert str(b) in report.missing


def test_added_file_reports_drift(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    claimed = manifest_for([a])

    b = _write(tmp_path, "b.py", "y = 2\n")
    report = detect_drift(claimed, [a, b])
    assert report.in_drift
    assert str(b) in report.added


def test_same_version_different_code_is_drift(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    b = _write(tmp_path, "b.py", "y = 2\n")
    manifest_a = manifest_for([a], version="v1.0.0")
    manifest_b = manifest_for([b], version="v1.0.0")
    assert manifest_a.release_hash != manifest_b.release_hash
    assert same_version_different_code(manifest_a, manifest_b)


def test_same_version_same_code_is_not_drift(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    manifest_a = manifest_for([a], version="v1.0.0")
    manifest_b = manifest_for([a], version="v1.0.0")
    assert not same_version_different_code(manifest_a, manifest_b)


def test_different_versions_not_flagged(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    b = _write(tmp_path, "b.py", "y = 2\n")
    manifest_a = manifest_for([a], version="v1.0.0")
    manifest_b = manifest_for([b], version="v2.0.0")
    assert not same_version_different_code(manifest_a, manifest_b)


def test_no_version_label_not_flagged(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "x = 1\n")
    b = _write(tmp_path, "b.py", "y = 2\n")
    manifest_a = manifest_for([a])
    manifest_b = manifest_for([b])
    assert not same_version_different_code(manifest_a, manifest_b)
