"""Obsidian bidirectional sync — core primitives (ADR-0078).

This module ships the *skeleton* of the daemon: normalizer, sidecar,
device-id, GateContext, Gate protocol, WatchLoop. The conform-write
and adapt-write paths consume these primitives in sibling tasks
(ADR-0078 conform-write / adapt-write), as does the secret-leak gate.

The daemon's lifecycle:

  1. ``--watch`` mode reads vault roots, registers inotify watches via
     ``WatchLoop``, and dispatches each save-event to an ``EditHandler``.
  2. The handler builds a ``GateContext`` from the event + source state.
  3. Gates run; on Hold the event is held + alerted; on Pass the
     write-side push is invoked (conform git commit + PR, or adapt
     doc-API push with parent_hash).
  4. Sidecar is updated on successful push.

Read-side mirror (the existing ``~/.local/bin/treadmill-obsidian-sync.sh``
30s poll) continues to handle the conform + adapt pulls; the daemon adds
*nothing* there. v1 keeps read-side as the existing shell script; future
work folds both into one Python process.

No DB, no HTTP, no LLM. Pure-Python, fully unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

logger = logging.getLogger("treadmill_local.obsidian_sync")


# ── Canonical normalization ─────────────────────────────────────────────────


class Normalizer:
    """Canonical text normalization for content-hash equality.

    Both the daemon and the server compute the SAME normalized form
    before hashing. Without this, every vault edit on a worker-committed
    doc would look like "source moved" — CRLF/LF differences and
    trailing-whitespace cleanup are the noise we eliminate.

    Rules (ADR-0078 §5):
      1. Line endings folded to LF (``\\r\\n`` and bare ``\\r`` → ``\\n``).
      2. Trailing whitespace stripped per line.
      3. Exactly one trailing newline at end-of-file (add one if missing,
         strip any extras).

    The rules are stateless; ``normalize`` is a pure function on str.
    """

    @staticmethod
    def normalize(text: str) -> str:
        # 1. LF only.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # 2. Trim trailing whitespace per line.
        lines = [line.rstrip() for line in text.split("\n")]
        # 3. Single trailing newline. ``split("\n")`` of a text ending in
        # ``\n`` yields a trailing empty string; we drop all empties at
        # the end and then re-add exactly one newline.
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines) + "\n"

    @staticmethod
    def sha256_hex(text: str) -> str:
        """Return the sha256 of the canonical-normalized form, hex."""
        return hashlib.sha256(
            Normalizer.normalize(text).encode("utf-8")
        ).hexdigest()


# ── Sidecar (per-host fast-path state) ─────────────────────────────────────


@dataclass
class SidecarEntry:
    """One per-doc entry in the sidecar JSON map."""

    sha256: str
    """Normalized-content sha256 of the last successfully-pushed version."""

    parent_hash_at_push: str
    """The source-side hash the daemon claimed as parent at the last push.
    On the next vault edit, this value is sent as ``parent_hash`` to the
    server-side gate. Mismatch → 409 → conflict event."""

    pushed_at: float
    """Unix timestamp (seconds) of the last successful push."""


class Sidecar:
    """Per-host fast-path state for race detection.

    Lives at ``~/.treadmill-docs/.sync-state.json`` as a flat JSON map:
    ``{"<source-key>/<doc-relpath>": {sha256, parent_hash_at_push, pushed_at}}``.

    The sidecar is a fast-path cache, NOT a truth source: the
    authoritative race-detection primitive is the server-side
    ``parent_hash`` gate. If the sidecar is missing or stale, the
    daemon falls back to fetching the current source-side hash before
    pushing — slower but correct. **The push never bypasses the
    source-side gate**, so a corrupted sidecar can never cause a
    stale-overwrite.

    Atomic writes via temp file + rename so a daemon crash mid-write
    doesn't corrupt the JSON.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, SidecarEntry]:
        """Read the sidecar JSON. Returns empty dict if missing or
        unreadable; the caller falls back to fetching source-side state."""
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "sidecar at %s is unreadable; treating as empty (will fall "
                "back to fetching source state on next push)",
                self.path,
            )
            return {}
        out: dict[str, SidecarEntry] = {}
        for key, value in raw.items():
            try:
                out[key] = SidecarEntry(
                    sha256=value["sha256"],
                    parent_hash_at_push=value["parent_hash_at_push"],
                    pushed_at=float(value["pushed_at"]),
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "sidecar entry %s malformed; skipping (will refetch "
                    "source state on next push for this doc)",
                    key,
                )
        return out

    def write(self, entries: dict[str, SidecarEntry]) -> None:
        """Atomic-replace the sidecar with the given map. Ensures the
        parent directory exists."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            serialized = {
                key: {
                    "sha256": e.sha256,
                    "parent_hash_at_push": e.parent_hash_at_push,
                    "pushed_at": e.pushed_at,
                }
                for key, e in entries.items()
            }
            tmp.write_text(json.dumps(serialized, indent=2), "utf-8")
            os.replace(tmp, self.path)

    def update_entry(self, key: str, entry: SidecarEntry) -> None:
        """Read-modify-write one entry. Convenience helper."""
        current = self.load()
        current[key] = entry
        self.write(current)


# ── Device identifier ───────────────────────────────────────────────────────


_DEVICE_ID_LENGTH = 16


def device_id(state_dir: Path) -> str:
    """Return this host's stable device identifier.

    Reads ``<state_dir>/device-id`` if present; otherwise mints one as
    ``<hostname>-<16-char-random>`` and persists it. The device_id ships
    in every push for multi-device attribution (see ADR-0078 §5 —
    revision history queries answer "which device wrote which
    revision").
    """
    path = state_dir / "device-id"
    if path.exists():
        return path.read_text("utf-8").strip()
    state_dir.mkdir(parents=True, exist_ok=True)
    minted = f"{socket.gethostname()}-{secrets.token_hex(_DEVICE_ID_LENGTH // 2)}"
    path.write_text(minted, "utf-8")
    return minted


# ── GateContext + Gate protocol ────────────────────────────────────────────


SourceKind = Literal["conform", "adapt"]


@dataclass
class GateContext:
    """The fixed bundle a gate receives. Built once per inotify event,
    then passed through every gate in sequence. Gates may read but not
    mutate fields.
    """

    vault_path: Path
    """Absolute path of the file that changed in the vault."""

    source_kind: SourceKind
    """``conform`` (git source) vs ``adapt`` (doc-API source)."""

    source_repo: str
    """Owner/name of the source repo. For conform this is the canonical
    treadmill slug; for adapt it's the onboarded repo's owner/name."""

    file_relpath: str
    """Path of the file relative to the source root (``plans/foo.md`` or
    ``adrs/0078-bar.md``)."""

    vault_content: str
    """Raw bytes-decoded content of the vault file at event time. Not
    normalized; gates that compare against source state must normalize
    via ``Normalizer.normalize`` themselves."""

    source_content: str | None
    """The source's current content, fetched fresh (git-show for conform,
    doc-API GET for adapt). ``None`` if the source has no record of this
    file (creation case — caught by the no-source gate)."""

    source_hash: str | None
    """The normalized sha256 of ``source_content`` (or ``None`` if no
    source record). Daemon uses this as the ``parent_hash`` for the push."""

    sidecar_entry: SidecarEntry | None
    """The sidecar's last-pushed record for this file, or ``None`` if
    the daemon hasn't pushed this file before (or sidecar is missing)."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Open-ended bag for gate implementations that need to carry
    cross-call state (e.g. the secret-leak gate's ``sensitive_strings``
    list read from RepoConfig). Daemon assembles this from sources
    before invoking the gate chain."""


@dataclass
class GateResult:
    """The outcome of one gate check."""

    decision: Literal["pass", "hold", "skip"]
    """``pass`` — gate has no objection, continue to next gate.
    ``hold`` — gate refuses the write; daemon emits ``obsidian_edit_held``
    and stops processing this event.
    ``skip`` — informational; gate doesn't apply to this event (e.g.
    ADR-immutability gate on a plan file)."""

    reason: str
    """Short slug identifying the gate + decision (e.g.
    ``filename_invalid``, ``adr_immutability_violated``, ``secret_leak``).
    Becomes ``payload.reason`` on the held event."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Gate-specific structured data attached to the held event. E.g.
    the secret-leak gate names which substring(s) matched; the
    ADR-immutability gate names the offending line numbers."""

    @classmethod
    def passed(cls, reason: str = "") -> "GateResult":
        return cls(decision="pass", reason=reason)

    @classmethod
    def held(cls, reason: str, **payload: Any) -> "GateResult":
        return cls(decision="hold", reason=reason, payload=payload)

    @classmethod
    def skipped(cls, reason: str = "") -> "GateResult":
        return cls(decision="skip", reason=reason)


class Gate(Protocol):
    """A gate is a callable that inspects ``GateContext`` and returns
    a ``GateResult``. Gates are pure functions of the context — they
    don't mutate fields, don't touch the filesystem, don't make network
    calls. State they need (RepoConfig, etc.) comes via
    ``GateContext.extras``.
    """

    name: str

    def check(self, ctx: GateContext) -> GateResult: ...


# ── Watch loop ──────────────────────────────────────────────────────────────


EditHandler = Callable[[Path], None]
"""A callable the WatchLoop dispatches inotify events to. Receives the
absolute path of the file that changed."""


class WatchLoop:
    """Inotify-driven watch loop over a list of vault roots.

    Polls inotify events (via inotify_simple when available; falls back
    to a simple stat-based poll if not) and dispatches each modify event
    to the registered handler.

    v1 design choice: the loop is single-threaded and runs the handler
    in-line. Long-running handlers will block subsequent events. A
    follow-up adds a bounded thread pool when we see real concurrency.

    The watch list is set at construction. Adding watches after start
    is a follow-up (multi-vault support).
    """

    def __init__(
        self,
        roots: list[Path],
        handler: EditHandler,
        *,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.roots = roots
        self.handler = handler
        self.poll_interval = poll_interval_seconds
        self._stop = threading.Event()
        # State for the fallback poll path: per-file mtime baseline.
        # Populated lazily on first scan so the loop doesn't fire on
        # the entire vault contents at startup.
        self._mtimes: dict[Path, float] = {}
        self._mtimes_initialized = False

    def stop(self) -> None:
        """Signal the run loop to exit at the next poll tick."""
        self._stop.set()

    def run(self) -> None:
        """Block until ``stop()`` is called. Dispatches each detected
        modify event to ``self.handler``."""
        # Prefer inotify_simple where available; fall back to mtime poll.
        try:
            from inotify_simple import INotify, flags  # type: ignore[import-not-found]
            self._run_inotify(INotify, flags)
        except ImportError:
            logger.info(
                "inotify_simple not installed; falling back to mtime-poll "
                "watch (works correctly, just less efficient at high event "
                "rates). Install inotify_simple for better latency."
            )
            self._run_poll()

    def _run_inotify(self, INotify: Any, flags: Any) -> None:
        notifier = INotify()
        wd_to_root: dict[int, Path] = {}
        mask = (
            flags.MODIFY | flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE
        )
        for root in self.roots:
            if not root.exists():
                logger.warning(
                    "watch root %s does not exist; skipping registration",
                    root,
                )
                continue
            for path in [root, *self._all_subdirs(root)]:
                try:
                    wd = notifier.add_watch(str(path), mask)
                    wd_to_root[wd] = path
                except OSError:
                    logger.exception(
                        "failed to register inotify watch at %s; continuing",
                        path,
                    )
        while not self._stop.is_set():
            events = notifier.read(timeout=int(self.poll_interval * 1000))
            for ev in events:
                root = wd_to_root.get(ev.wd)
                if root is None:
                    continue
                name = getattr(ev, "name", "") or ""
                if not name.endswith(".md"):
                    # We only care about markdown files (plans + adrs).
                    continue
                changed = root / name
                try:
                    self.handler(changed)
                except Exception:
                    logger.exception(
                        "handler raised on %s; continuing", changed,
                    )

    def _run_poll(self) -> None:
        while not self._stop.is_set():
            self._scan_once()
            time.sleep(self.poll_interval)

    def _scan_once(self) -> None:
        """One pass over all vault roots, dispatching any md file whose
        mtime moved forward since the last scan. First scan establishes
        the baseline silently (no dispatch)."""
        current: dict[Path, float] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                try:
                    current[path] = path.stat().st_mtime
                except OSError:
                    continue
        if not self._mtimes_initialized:
            self._mtimes = current
            self._mtimes_initialized = True
            return
        for path, mtime in current.items():
            prev = self._mtimes.get(path)
            if prev is None or mtime > prev:
                try:
                    self.handler(path)
                except Exception:
                    logger.exception(
                        "handler raised on %s; continuing", path,
                    )
        self._mtimes = current

    @staticmethod
    def _all_subdirs(root: Path) -> list[Path]:
        """List subdirectories of ``root`` recursively. Used to register
        inotify watches on the whole tree (inotify is per-directory)."""
        out: list[Path] = []
        for entry in root.rglob("*"):
            if entry.is_dir():
                out.append(entry)
        return out
