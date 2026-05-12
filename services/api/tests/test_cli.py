"""Unit tests for treadmill_api.cli — startup migration + logging hooks.

The CLI entrypoint added two responsibilities in Week 4 (dev-local deploy
friction points #1 and #2):

* ``_run_migrations`` invokes ``alembic upgrade head`` in-process when
  ``settings.skip_migrations`` is False, and is a no-op when True.
* ``run()`` calls ``logging.basicConfig`` so ``treadmill_api.*`` INFO
  surfaces — the level is sourced from ``Settings.log_level``.

These tests exercise ``_run_migrations`` directly with a fake Settings.
Full ``run()`` execution is not exercised here because it calls
``uvicorn.run`` (a blocking server). The logging-basicConfig coverage
lives indirectly via ``test_config.py``'s ``log_level`` parsing test plus
this file's documentation of intent.
"""

from __future__ import annotations

from unittest.mock import patch

from treadmill_api.cli import _run_migrations
from treadmill_api.config import Settings


def test_run_migrations_invokes_alembic_when_not_skipped():
    """With ``skip_migrations=False`` (default), ``_run_migrations`` calls
    ``alembic.command.upgrade(cfg, "head")`` exactly once. We mock both the
    Config constructor and the upgrade call so the test doesn't need a real
    alembic.ini on disk relative to the test CWD."""
    settings = Settings(skip_migrations=False)
    with (
        patch("alembic.config.Config") as mock_config,
        patch("alembic.command.upgrade") as mock_upgrade,
    ):
        _run_migrations(settings)
    mock_config.assert_called_once_with("alembic.ini")
    mock_upgrade.assert_called_once()
    # Second positional arg is the revision target.
    args, _ = mock_upgrade.call_args
    assert args[1] == "head"


def test_run_migrations_skipped_when_flag_set():
    """With ``skip_migrations=True``, ``_run_migrations`` returns without
    importing or invoking alembic. The alembic module isn't even imported
    in this code path, so patching it would be misleading — instead we
    confirm the function returns cleanly and short-circuits the call."""
    settings = Settings(skip_migrations=True)
    with (
        patch("alembic.config.Config") as mock_config,
        patch("alembic.command.upgrade") as mock_upgrade,
    ):
        _run_migrations(settings)
    mock_config.assert_not_called()
    mock_upgrade.assert_not_called()
