"""Unit tests for RepoConfig.worker_hints_enabled (ADR-0081)."""

from treadmill_api.repo_config import RepoConfig, parse_repo_config, to_dict


def test_worker_hints_enabled_defaults_true() -> None:
    """worker_hints_enabled defaults to True."""
    config = RepoConfig(repo="test/repo")
    assert config.worker_hints_enabled is True


def test_worker_hints_enabled_explicit_false() -> None:
    """worker_hints_enabled can be set to False."""
    config = RepoConfig(repo="test/repo", worker_hints_enabled=False)
    assert config.worker_hints_enabled is False


def test_worker_hints_enabled_explicit_true() -> None:
    """worker_hints_enabled can be set to True explicitly."""
    config = RepoConfig(repo="test/repo", worker_hints_enabled=True)
    assert config.worker_hints_enabled is True


def test_parse_repo_config_hints_true() -> None:
    """parse_repo_config extracts worker_hints_enabled=true."""
    data = {
        "repo": "test/repo",
        "worker_hints_enabled": True,
    }
    config = parse_repo_config(data)
    assert config.worker_hints_enabled is True


def test_parse_repo_config_hints_false() -> None:
    """parse_repo_config extracts worker_hints_enabled=false."""
    data = {
        "repo": "test/repo",
        "worker_hints_enabled": False,
    }
    config = parse_repo_config(data)
    assert config.worker_hints_enabled is False


def test_parse_repo_config_hints_defaults_true() -> None:
    """parse_repo_config defaults to True when hints field absent."""
    data = {"repo": "test/repo"}
    config = parse_repo_config(data)
    assert config.worker_hints_enabled is True


def test_to_dict_includes_hints_true() -> None:
    """to_dict includes worker_hints_enabled=true."""
    config = RepoConfig(repo="test/repo", worker_hints_enabled=True)
    result = to_dict(config)
    assert result["worker_hints_enabled"] is True


def test_to_dict_includes_hints_false() -> None:
    """to_dict includes worker_hints_enabled=false."""
    config = RepoConfig(repo="test/repo", worker_hints_enabled=False)
    result = to_dict(config)
    assert result["worker_hints_enabled"] is False


def test_round_trip_hints_true() -> None:
    """Parse → to_dict → parse round-trip preserves hints=true."""
    data = {"repo": "test/repo", "worker_hints_enabled": True}
    config1 = parse_repo_config(data)
    dict1 = to_dict(config1)
    config2 = parse_repo_config(dict1)
    assert config2.worker_hints_enabled is True


def test_round_trip_hints_false() -> None:
    """Parse → to_dict → parse round-trip preserves hints=false."""
    data = {"repo": "test/repo", "worker_hints_enabled": False}
    config1 = parse_repo_config(data)
    dict1 = to_dict(config1)
    config2 = parse_repo_config(dict1)
    assert config2.worker_hints_enabled is False
