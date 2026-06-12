"""Deploy watcher — monitors the deploy-events SQS queue and reconciles local containers.

The watcher runs as a subprocess of ``treadmill-local up``. It long-polls the
deploy_events SQS queue, parses SNS-wrapped Treadmill events, and applies
changes to local containers based on a dispatch table keyed by file path globs.

Dispatch table (first-match wins; order is significant):
  - ``services/api/**``       → api       (docker build + RECREATE container + health check)
  - ``services/dashboard/**`` → dashboard (RECREATE container — build path lives in the runtime helper)
  - ``workers/agent/**``      → agent     (docker build only; workers are one-shot per ADR-0018)
  - ``infra/**``              → infra     (notify-only; do NOT shell out)
  - ``tools/local-adapter/**`` → adapter   (sync local clone + restart host sibling processes per ADR-0069)
  - other                     → ignored

The class is split from its subprocess entrypoint deliberately.
``DeployWatcher`` takes injectable callables for SQS, the GitHub API, and the
"recreate the API container from the freshly-built image" runtime helper so
it can be unit-tested without real AWS, network, or docker access. Subprocess
calls (``docker build``) are made via the standard ``subprocess`` module and
can be patched in tests.

The ``main()`` function is the production wiring that constructs real
callables against boto3 / the GitHub API / ``LocalRuntime``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("treadmill.deploy_watcher")


# ── Dispatch table ────────────────────────────────────────────────────────────
#
# Each entry is (path_prefix, category). First-match wins: a file under
# ``infra/observability/`` matches "infra" before any other rule.
# The "/**" glob suffix is implicit — we use startswith matching.

_DISPATCH_TABLE: list[tuple[str, str]] = [
    ("services/api/", "api"),
    ("services/dashboard/", "dashboard"),
    ("workers/agent/", "agent"),
    ("infra/", "infra"),
    ("tools/local-adapter/", "adapter"),
]

# ADR-0069: ``adapter`` was previously notify-only. After the staleness
# self-heal landed it gained an explicit action — sync the local clone
# to origin/main and restart the autoscaler + scheduler siblings via the
# injected ``restart_host_processes_fn`` — so a ``tools/local-adapter/**``
# merge propagates within one watcher iteration instead of waiting for
# the operator to bounce ``treadmill-local up``.
_NOTIFY_ONLY_CATEGORIES: frozenset[str] = frozenset({"infra"})

_POLL_WAIT_SECONDS: int = 20
_HEALTH_TIMEOUT_SECONDS: int = 30


def _categorize_file(path: str) -> str | None:
    """Return the category for a file path, or None to ignore."""
    for prefix, category in _DISPATCH_TABLE:
        if path.startswith(prefix):
            return category
    return None


def _categorize_files(paths: list[str]) -> dict[str, list[str]]:
    """Group file paths by category. Ignores uncategorized paths."""
    by_category: dict[str, list[str]] = {}
    for path in paths:
        cat = _categorize_file(path)
        if cat is not None:
            by_category.setdefault(cat, []).append(path)
    return by_category


# ── Core watcher class ────────────────────────────────────────────────────────


class DeployWatcher:
    """Event-driven watcher that reconciles local containers from deploy events.

    Takes injectable callables for its three external dependencies — SQS, the
    GitHub PR files API, and the "recreate the API container from the
    freshly-built image" runtime helper — so unit tests can operate without
    real AWS, network, or docker access. The remaining subprocess call
    (``docker build``) goes through the standard ``subprocess`` module and is
    patchable in tests.

    ``api_health_url`` is the URL ``_action_api`` polls after the recreate to
    confirm the new container has come up healthy. Production wiring derives
    it from ``cfg["local"]["api_url"]`` (the dev-local deployment YAML); tests
    pass any URL they want and patch ``_wait_healthy`` to no-op.
    """

    def __init__(
        self,
        *,
        receive_fn: Callable[[], list[dict[str, Any]]],
        ack_fn: Callable[[str], None],
        get_pr_files_fn: Callable[[int], list[str] | None],
        recreate_api_fn: Callable[[str], None],
        recreate_dashboard_fn: Callable[[], None],
        api_health_url: str,
        state_file: Path,
        repo_root: Path,
        restart_host_processes_fn: Callable[[], None] | None = None,
        staleness_guard: Any = None,
        staleness_pid_file: Path | None = None,
    ) -> None:
        self._receive_fn = receive_fn
        self._ack_fn = ack_fn
        self._get_pr_files_fn = get_pr_files_fn
        self._recreate_api_fn = recreate_api_fn
        self._recreate_dashboard_fn = recreate_dashboard_fn
        self._api_health_url = api_health_url
        self._state_file = state_file
        self._repo_root = repo_root
        # ADR-0069 accelerator: the ``adapter`` category invokes this
        # after ``_sync_local_to_origin`` so a ``tools/local-adapter/**``
        # merge restarts the autoscaler + scheduler within the same
        # watcher iteration. Wired by the deploy-watcher's ``main()``
        # against ``LocalRuntime.restart_host_processes``; ``None``
        # disables the accelerator (legacy/test paths log + skip).
        self._restart_host_processes_fn = restart_host_processes_fn
        # ADR-0069 self-heal: when set, ``run()`` re-execs the watcher
        # at each loop head before processing messages, so a stale
        # watcher heals BEFORE running the adapter-category action.
        self._staleness_guard = staleness_guard
        self._staleness_pid_file = staleness_pid_file
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Poll until stop() is called or a signal is received."""
        from treadmill_local.staleness import maybe_reexec
        from treadmill_local.subprocess_logging import RateLimitedErrorLogger
        # Rate-limit the loop's error path so a persistent failure
        # (queue unreachable, GitHub auth expired) doesn't dump a full
        # traceback every iteration. First occurrence logs in full;
        # repeats are summarized; ``reset()`` after a clean poll
        # re-arms a fresh traceback for the next incident.
        error_logger = RateLimitedErrorLogger(logger)
        logger.info("deploy watcher starting (state=%s)", self._state_file)
        while not self._stop_event.is_set():
            # ADR-0069 safe-point: top of loop, BETWEEN event batches.
            # Re-exec here (never mid-handler) so a stale watcher heals
            # before running the adapter-category accelerator that
            # would otherwise restart its siblings with the OLD code's
            # idea of how to restart them.
            maybe_reexec(self._staleness_guard, self._staleness_pid_file)
            try:
                messages = self._receive_fn()
                for msg in messages:
                    self._process_message(msg)
                error_logger.reset()
            except Exception as exc:
                error_logger.log(exc, "poll iteration failed; continuing")
        logger.info("deploy watcher stopped")

    def stop(self) -> None:
        self._stop_event.set()

    # ── Message processing ────────────────────────────────────────────────────

    def _process_message(self, message: dict[str, Any]) -> None:
        receipt = message["ReceiptHandle"]

        # SQS body is an SNS notification; inner Message is the Treadmill event
        # record produced by ``eventbus._build_record``. Its shape is::
        #
        #   {"event_id", "entity_type", "action",
        #    "task_id", "plan_id", "run_id", "step_id",
        #    "payload": {<typed-payload fields>}}
        #
        # For ``github.pr_merged`` (the only action subscribed via the SNS
        # filter policy on the deploy-events topic), the inner ``payload``
        # carries ``pr_number`` + ``merged_sha`` per ``GithubPrMerged``.
        sns_notification = json.loads(message["Body"])
        record = json.loads(sns_notification["Message"])
        payload = record.get("payload") or {}

        try:
            pr_number: int = payload["pr_number"]
            merge_commit_sha: str = payload["merged_sha"]
        except KeyError as exc:
            # Malformed message — log + ack so we don't re-receive into the DLQ
            # forever. The filter policy is supposed to gate this to pr_merged
            # only, but a schema drift on the producer side shouldn't wedge the
            # watcher; surface clearly + move on.
            logger.error(
                "skipping malformed deploy-event (missing %s); record=%s",
                exc, record,
            )
            self._ack_fn(receipt)
            return

        logger.info("processing pr=#%d sha=%.8s", pr_number, merge_commit_sha)

        files = self._get_pr_files_fn(pr_number)
        if files is None:
            logger.warning("pr #%d not found (deleted?); acking and skipping", pr_number)
            self._ack_fn(receipt)
            return

        by_category = _categorize_files(files)
        if not by_category:
            logger.info("no relevant files in pr #%d; acking", pr_number)
            self._ack_fn(receipt)
            return

        state = self._load_state()

        # Iterate in dispatch-table order so actions run api → agent → infra → adapter.
        for _, category in _DISPATCH_TABLE:
            if category not in by_category:
                continue
            if state.get(category) == merge_commit_sha:
                logger.info(
                    "category=%s sha=%.8s already applied; skipping",
                    category, merge_commit_sha,
                )
                continue
            # May raise — if so, we do NOT ack; SQS re-delivers (maxReceiveCount=3→DLQ).
            self._run_action(category, by_category[category])
            state[category] = merge_commit_sha
            self._save_state(state)

        self._ack_fn(receipt)

    def _run_action(self, category: str, files: list[str]) -> None:
        if category == "api":
            self._action_api()
        elif category == "dashboard":
            self._action_dashboard()
        elif category == "agent":
            self._action_agent()
        elif category == "adapter":
            self._action_adapter()
        elif category in _NOTIFY_ONLY_CATEGORIES:
            self._action_notify(category, files)
        else:
            logger.warning("unknown category %r; skipping", category)

    # ── Category actions ──────────────────────────────────────────────────────

    def _sync_local_to_origin(self) -> None:
        # Sibling to ADR-0024: ``docker build`` packages the LOCAL working tree,
        # so if the operator's clone is behind ``origin/main`` the "fresh"
        # image is built from pre-merge source — the same silent-no-op shape
        # the recreate fix retired, one layer down. Fast-forward the clone
        # before building. ff-only failure (unpushed work, different branch
        # checked out) warns + falls back to current local state to preserve
        # today's deploy behavior; no new silent-skip vector for normal
        # merges.
        repo_root_str = str(self._repo_root)
        fetch = subprocess.run(
            ["git", "-C", repo_root_str, "fetch", "origin", "--quiet"],
            check=False,
        )
        if fetch.returncode != 0:
            logger.warning(
                "git fetch origin failed (rc=%d); building from current local state",
                fetch.returncode,
            )
            return
        merge = subprocess.run(
            ["git", "-C", repo_root_str, "merge", "--ff-only", "origin/main"],
            check=False, capture_output=True, text=True,
        )
        if merge.returncode != 0:
            logger.warning(
                "git merge --ff-only origin/main failed (rc=%d); "
                "building from current local state. stderr=%s",
                merge.returncode, (merge.stderr or "").strip(),
            )
            return
        head = subprocess.run(
            ["git", "-C", repo_root_str, "rev-parse", "--short", "HEAD"],
            check=False, capture_output=True, text=True,
        )
        logger.info(
            "synced local repo to origin/main (HEAD=%s)",
            (head.stdout or "").strip(),
        )

    def _action_api(self) -> None:
        # ``docker restart`` re-runs the EXISTING container's image, so the
        # freshly-built ``treadmill-api:dev`` would never go live — the
        # silent-no-op the operator captured ADR-0024 to retire. We
        # ``docker build`` the new image, then call the injected
        # ``recreate_api_fn`` which force-removes the running container
        # and ``docker run``s a new one from the just-built image with the
        # same env/ports/network as the original ``up`` boot
        # (``LocalRuntime.recreate_api_container``, sharing
        # ``_build_api_service_spec`` so the two creation paths can't drift).
        self._sync_local_to_origin()
        api_dir = self._repo_root / "services" / "api"
        subprocess.run(
            ["docker", "build", "-t", "treadmill-api:dev", str(api_dir)],
            check=True,
        )
        # Digest-pin (task c62f097d): resolve the image ID the build we
        # JUST ran produced and recreate from THAT — never from whatever
        # ``:dev`` happens to resolve to at run time. A migration-carrying
        # merge stamps the DB moments before the recreate; a stale tag
        # (raced rebuild, parallel actor, old watcher process) then boots
        # pre-merge code against the post-merge schema and alembic
        # crashloops on Can't-locate-revision — the 2026-06-11 8h54m loop
        # outage, deterministic on every schema merge.
        built_image_id = subprocess.run(
            [
                "docker", "image", "inspect", "treadmill-api:dev",
                "--format", "{{.Id}}",
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        logger.info("api image built: %s", built_image_id)
        self._recreate_api_fn(built_image_id)
        self._wait_healthy(
            self._api_health_url,
            timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
        )
        logger.info("api container recreated from new image and healthy")

    def _action_dashboard(self) -> None:
        # Mirror of ``_action_api`` for ``services/dashboard/**`` PR merges
        # (ADR-0056). The runtime helper owns the rebuild path (it calls
        # ``_ensure_images_built``, which already knows how to build
        # ``treadmill-dashboard:dev``), so the watcher only syncs the local
        # clone to origin/main and dispatches; the helper then
        # force-removes the running container and ``docker run``s a new
        # one from the freshly-tagged image via
        # ``_build_dashboard_service_spec``. Dashboard is static nginx;
        # no health probe here (sibling to the API's
        # ``/health/ready`` check) — keep this path simple.
        self._sync_local_to_origin()
        self._recreate_dashboard_fn()
        logger.info("dashboard container recreated from new image")

    def _action_agent(self) -> None:
        self._sync_local_to_origin()
        dockerfile = self._repo_root / "workers" / "agent" / "Dockerfile"
        subprocess.run(
            [
                "docker", "build",
                "-t", "treadmill-agent:dev",
                str(self._repo_root),
                "-f", str(dockerfile),
            ],
            check=True,
        )
        logger.info("agent image rebuilt (workers are one-shot; no restart needed)")

    def _action_adapter(self) -> None:
        # ADR-0069 accelerator: a ``tools/local-adapter/**`` merge means
        # the autoscaler + scheduler + this watcher are all running on
        # pre-merge bytes. The watcher self-healed at the loop head
        # (it's running new bytes by the time this method is reached);
        # this action propagates the new source to the SIBLINGS by
        # syncing the local clone to origin/main and then stopping +
        # restarting the autoscaler and scheduler through the runtime
        # helpers. Without the injected ``restart_host_processes_fn``
        # (legacy/test paths) the action falls back to the pre-ADR-0069
        # notify-only behavior so the watcher itself stays useful.
        self._sync_local_to_origin()
        if self._restart_host_processes_fn is None:
            logger.info(
                "adapter merge: no restart_host_processes_fn wired; "
                "siblings will self-heal at their own next tick"
            )
            return
        self._restart_host_processes_fn()
        logger.info(
            "adapter merge: autoscaler + scheduler restarted from new source"
        )

    def _action_notify(self, category: str, files: list[str]) -> None:
        logger.info(
            "deploy event: category=%s requires manual action; affected_files=%s",
            category,
            files,
        )

    # ── Health check ──────────────────────────────────────────────────────────

    def _wait_healthy(self, url: str, *, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError(f"container not healthy at {url} after {timeout_seconds}s")

    # ── State file ────────────────────────────────────────────────────────────

    def _load_state(self) -> dict[str, str]:
        if self._state_file.exists():
            return json.loads(self._state_file.read_text())
        return {}

    def _save_state(self, state: dict[str, str]) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(state, indent=2) + "\n")


# ── Subprocess entrypoint ─────────────────────────────────────────────────────


def main() -> int:
    """Production entrypoint: load deployment config, wire real callables, run.

    Reads ``TREADMILL_DEPLOY_WATCHER_DEPLOYMENT_ID`` to locate the deployment
    YAML (``~/.treadmill/<deployment_id>.yaml``) and extract the
    ``deploy_events_queue_url``. When the env var is unset, falls back to
    ``TREADMILL_DEPLOY_WATCHER_QUEUE_URL`` for fully-local / test scenarios.

    GitHub credentials come from ``GITHUB_TOKEN`` (per ADR-0019).
    Repo coordinates come from ``GITHUB_OWNER`` + ``GITHUB_REPO``.
    The repo root on disk comes from ``TREADMILL_REPO_ROOT``.
    """
    # Imports are local to keep DeployWatcher itself dependency-free for tests.
    import boto3

    from treadmill_local.runtime import (
        DEPLOY_WATCHER_LOG_FILE,
        DEPLOY_WATCHER_PID_FILE,
        STATE_DIR,
    )
    from treadmill_local.staleness import StalenessGuard
    from treadmill_local.subprocess_logging import configure_rotating_logging

    # The subprocess owns its own log file — the parent passes the
    # path via env. Fall back to the package default if unset so a
    # bare ``python -m treadmill_local.deploy_watcher`` still has
    # somewhere to write.
    log_file_env = os.environ.get("TREADMILL_DEPLOY_WATCHER_LOG_FILE")
    log_file = Path(log_file_env) if log_file_env else DEPLOY_WATCHER_LOG_FILE
    configure_rotating_logging(log_file)

    # ADR-0069: rewrite the PID file with our own pid at startup so a
    # re-exec'd process owns the pid file the parent uses for
    # ``_pid_alive`` + ``_stop_deploy_watcher``.
    STATE_DIR.mkdir(exist_ok=True)
    DEPLOY_WATCHER_PID_FILE.write_text(str(os.getpid()))

    github_owner = os.environ["GITHUB_OWNER"]
    github_repo_name = os.environ["GITHUB_REPO"]
    repo_root = Path(os.environ["TREADMILL_REPO_ROOT"])

    # ADR-0049 GitHub auth. ``app`` mode mints a short-lived installation
    # token from the API per call (the App private key stays on the API;
    # tokens expire ~1h, so the long-running watcher fetches fresh each time).
    # ``pat`` mode uses the injected personal PAT (legacy, pre-decommission).
    github_auth_mode = os.environ.get("GITHUB_AUTH_MODE", "pat")
    if github_auth_mode == "app":
        _api_url = os.environ["TREADMILL_API_URL"].rstrip("/")
        _repo_slug = f"{github_owner}/{github_repo_name}"

        def _resolve_token() -> str:
            req = urllib.request.Request(
                _api_url + "/api/v1/github/installation-token",
                data=json.dumps({"repo": _repo_slug}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())["token"]
    else:
        _pat = os.environ["GITHUB_TOKEN"]

        def _resolve_token() -> str:
            return _pat

    deployment_id = os.environ.get("TREADMILL_DEPLOY_WATCHER_DEPLOYMENT_ID")
    cfg: dict[str, Any] | None
    if deployment_id is not None:
        from treadmill_local.deployment_config import load_deployment_yaml
        cfg = load_deployment_yaml(deployment_id)
        queue_url: str = cfg["aws"]["deploy_events_queue_url"]
    else:
        cfg = None
        queue_url = os.environ["TREADMILL_DEPLOY_WATCHER_QUEUE_URL"]

    sqs = boto3.client(
        "sqs",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

    # State file lives next to the rest of the .treadmill-local state under
    # the repo root. The parent runtime spawns the watcher with cwd anchored
    # to the repo root (see runtime.py spawn cwd + cli.py typer callback),
    # so this relative path resolves there.
    state_file = Path(".treadmill-local") / "deploy-watcher-state.json"

    def receive() -> list[dict[str, Any]]:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            WaitTimeSeconds=_POLL_WAIT_SECONDS,
            MaxNumberOfMessages=1,
        )
        return resp.get("Messages", [])

    def ack(receipt_handle: str) -> None:
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)

    def get_pr_files(pr_number: int) -> list[str] | None:
        url = (
            f"https://api.github.com/repos/{github_owner}/{github_repo_name}"
            f"/pulls/{pr_number}/files"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {_resolve_token()}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
                return [f["filename"] for f in data]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    # Wire the runtime helper that shares the API-container creation path
    # with ``treadmill-local up`` — calling ``recreate_api_container`` here
    # force-removes the existing container and ``docker run``s a new one
    # from the freshly-built image with the same env/ports/network. In
    # fully-local (no-deployment-id) mode the watcher has no ``LocalRuntime``
    # to drive and the API action is a no-op — that path only exists for
    # legacy/test scenarios; production ``services/api/**`` PR merges always
    # come through the dev-local watcher.
    restart_host_processes_fn: Callable[[], None] | None
    if cfg is not None:
        from treadmill_local.runtime import LocalRuntime
        runtime = LocalRuntime(
            infra_dir=repo_root / "infra",
            deployment_config=cfg,
        )

        def recreate_api(built_image_id: str) -> None:
            runtime.recreate_api_container(expected_image_id=built_image_id)

        def recreate_dashboard() -> None:
            runtime.recreate_dashboard_container()

        # ADR-0069 accelerator: a ``tools/local-adapter/**`` merge
        # restarts the autoscaler + scheduler so they pick up the
        # new bytes immediately. The injected callable goes through
        # the runtime helper so the watcher class itself never
        # imports runtime internals — same pattern as the
        # ``recreate_*`` closures above.
        def restart_host_processes() -> None:
            runtime.restart_host_processes()

        restart_host_processes_fn = restart_host_processes

        api_health_url = (
            cfg["local"]["api_url"].rstrip("/") + "/health/ready"
        )
    else:
        def recreate_api(built_image_id: str) -> None:
            logger.warning(
                "API recreate requested without a deployment_id; "
                "no-op (fully-local / test mode). built_image_id=%s",
                built_image_id,
            )

        def recreate_dashboard() -> None:
            logger.warning(
                "dashboard recreate requested without a deployment_id; "
                "no-op (fully-local / test mode)."
            )

        # No runtime instance in fully-local/test mode → the watcher
        # falls back to logging the merge without restarting siblings.
        restart_host_processes_fn = None
        api_health_url = "http://localhost:8088/health/ready"

    # ADR-0069: fingerprint our package bytes at startup; the loop
    # head consults this each iteration to decide whether to re-exec.
    staleness_guard = StalenessGuard(module="treadmill_local.deploy_watcher")

    watcher = DeployWatcher(
        receive_fn=receive,
        ack_fn=ack,
        get_pr_files_fn=get_pr_files,
        recreate_api_fn=recreate_api,
        recreate_dashboard_fn=recreate_dashboard,
        api_health_url=api_health_url,
        state_file=state_file,
        repo_root=repo_root,
        restart_host_processes_fn=restart_host_processes_fn,
        staleness_guard=staleness_guard,
        staleness_pid_file=DEPLOY_WATCHER_PID_FILE,
    )

    def _on_signal(_signum: int, _frame: Any) -> None:
        logger.info("received signal; stopping")
        watcher.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    watcher.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
