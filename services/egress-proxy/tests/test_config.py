"""Tests for ConfigStore load, mtime-based reload, and missing-IP behavior."""

from __future__ import annotations

import json
import time

import pytest

from treadmill_egress_proxy.config import ConfigStore, WorkerAllowlist


def _write_config(path, data):
    path.write_text(json.dumps(data))


def _valid_config(worker_ip: str = "10.0.0.1") -> dict:
    return {
        "worker_ip": worker_ip,
        "always_allowed": ["api.anthropic.com"],
        "install_allowed": ["pypi.org"],
        "install_credential_hash": "a" * 64,
    }


def test_load_single_config(tmp_path):
    _write_config(tmp_path / "worker1.json", _valid_config("10.0.0.1"))
    store = ConfigStore(tmp_path)
    result = store.get("10.0.0.1")
    assert result is not None
    assert result.worker_ip == "10.0.0.1"
    assert "api.anthropic.com" in result.always_allowed
    assert "pypi.org" in result.install_allowed


def test_missing_ip_returns_none(tmp_path):
    _write_config(tmp_path / "worker1.json", _valid_config("10.0.0.1"))
    store = ConfigStore(tmp_path)
    assert store.get("10.0.0.99") is None


def test_empty_directory_returns_none(tmp_path):
    store = ConfigStore(tmp_path)
    assert store.get("10.0.0.1") is None


def test_mtime_reload(tmp_path):
    cfg_path = tmp_path / "worker1.json"
    _write_config(cfg_path, _valid_config("10.0.0.1"))
    store = ConfigStore(tmp_path)
    result1 = store.get("10.0.0.1")
    assert result1 is not None

    # Update the file with a new always_allowed entry and bump mtime
    time.sleep(0.01)
    updated = _valid_config("10.0.0.1")
    updated["always_allowed"] = ["api.anthropic.com", "api.github.com"]
    _write_config(cfg_path, updated)
    # Force a different mtime
    new_mtime = cfg_path.stat().st_mtime + 1
    import os
    os.utime(cfg_path, (new_mtime, new_mtime))

    result2 = store.get("10.0.0.1")
    assert result2 is not None
    assert "api.github.com" in result2.always_allowed


def test_stale_entry_removed(tmp_path):
    cfg_path = tmp_path / "worker1.json"
    _write_config(cfg_path, _valid_config("10.0.0.1"))
    store = ConfigStore(tmp_path)
    assert store.get("10.0.0.1") is not None

    cfg_path.unlink()
    assert store.get("10.0.0.1") is None


def test_invalid_json_skipped(tmp_path):
    (tmp_path / "bad.json").write_text("not json")
    _write_config(tmp_path / "good.json", _valid_config("10.0.0.2"))
    store = ConfigStore(tmp_path)
    assert store.get("10.0.0.2") is not None
    assert store.get("10.0.0.1") is None


def test_pydantic_validation_extra_fields_rejected(tmp_path):
    bad = _valid_config("10.0.0.1")
    bad["unexpected_field"] = "oops"
    (tmp_path / "worker1.json").write_text(json.dumps(bad))
    store = ConfigStore(tmp_path)
    # extra="forbid" means validation fails — file is skipped
    assert store.get("10.0.0.1") is None


def test_worker_allowlist_model_extra_forbidden():
    with pytest.raises(Exception):
        WorkerAllowlist(
            worker_ip="1.2.3.4",
            always_allowed=[],
            install_allowed=[],
            install_credential_hash="a" * 64,
            extra_bad_field="x",
        )
