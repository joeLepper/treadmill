"""GitHub auth bootstrap for the worker.

Per ADR-0019, the worker's AWS credentials arrive as
``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` env vars injected by
the local-adapter at container-spawn time. The worker never authenticates
with SSO and never fetches its own credentials secret.

When ``REPO_MODE=github`` the worker authenticates to GitHub via ``gh``'s
keyring. Two modes (ADR-0049):

  * ``GITHUB_AUTH_MODE=pat`` (legacy): fetch the personal PAT from Secrets
    Manager and hand it to ``gh``.
  * ``GITHUB_AUTH_MODE=app``: ask the API to mint a short-lived GitHub App
    installation token (the App private key stays on the API, never on the
    worker) and hand *that* to ``gh``. This is what lets the personal PAT be
    decommissioned.

Either way the token is handed to ``gh`` via stdin so it never appears in
argv, env, or git URLs, then dropped.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import boto3

from treadmill_api.models.onboarding import WorkerDeps

if TYPE_CHECKING:
    from treadmill_agent.config import Settings

logger = logging.getLogger("treadmill.agent.startup_auth")


class StartupAuthError(RuntimeError):
    """Worker bootstrap failed in a way the runner cannot recover from."""


@dataclass(frozen=True)
class ClaudeCreds:
    """Resolved Claude credential for a step (ADR-0055, ADR-0066).

    Returned by :func:`fetch_claude_credentials`; consumed by
    :func:`treadmill_agent.claude_code.build_claude_env` to set
    ``CLAUDE_CODE_OAUTH_TOKEN`` (``oauth``) or ``ANTHROPIC_API_KEY``
    (``api_key``) on the Claude Code subprocess env.

    ``fallback`` (ADR-0066) is the operator-configured secondary account
    the worker swaps in when the primary's Claude Code subprocess exits
    with a usage-limit signature. ``None`` when the repo did not opt in
    or the resolver could not produce a fallback credential.
    """

    account: str
    type: Literal["oauth", "api_key"]
    token: str
    fallback: "ClaudeCreds | None" = None


def fetch_claude_credentials(
    *, settings: "Settings", repo: str
) -> ClaudeCreds | None:
    """Resolve the Claude credential for ``repo`` via the API (ADR-0055).

    POSTs ``/api/v1/claude/credentials {repo}``. Returns the resolved
    ``ClaudeCreds`` on 200. Returns ``None`` on 503 — feature unconfigured,
    so the worker falls back to the existing ``CLAUDE_CREDENTIALS_PATH``
    bind-mount (backward compatibility with unmigrated deployments).
    Any other failure raises ``StartupAuthError`` so the step fails cleanly
    rather than silently routing to the wrong account.
    """
    url = settings.api_url.rstrip("/") + "/api/v1/claude/credentials"
    body = json.dumps({"repo": repo}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            logger.info(
                "claude credential resolver returned 503 (feature off); "
                "falling back to mounted credentials",
            )
            return None
        raise StartupAuthError(
            f"claude credential resolver returned {exc.code} for repo={repo!r}: "
            f"{exc.reason}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise StartupAuthError(
            f"failed to fetch claude credentials via {url}: {exc}"
        ) from exc
    try:
        # ADR-0066: an optional nested ``fallback`` block on the response
        # carries the operator's secondary account credential. Build the
        # nested ``ClaudeCreds`` (without further nesting) when present.
        fallback_raw = payload.get("fallback")
        fallback_creds: ClaudeCreds | None = None
        if fallback_raw is not None:
            fallback_creds = ClaudeCreds(
                account=fallback_raw["account"],
                type=fallback_raw["type"],
                token=fallback_raw["token"],
                fallback=None,
            )
        creds = ClaudeCreds(
            account=payload["account"],
            type=payload["type"],
            token=payload["token"],
            fallback=fallback_creds,
        )
    except (KeyError, TypeError) as exc:
        raise StartupAuthError(
            f"claude credential response malformed: missing field {exc}"
        ) from exc
    # Never log the token; account+type identifies the routing decision.
    # ADR-0066: log presence + account/type of the fallback (but not its
    # token) so the operator can confirm the resolver populated it.
    if creds.fallback is not None:
        logger.info(
            "fetched claude credential: account=%s type=%s repo=%s "
            "fallback_account=%s fallback_type=%s",
            creds.account, creds.type, repo,
            creds.fallback.account, creds.fallback.type,
        )
    else:
        logger.info(
            "fetched claude credential: account=%s type=%s repo=%s",
            creds.account, creds.type, repo,
        )
    return creds


def fetch_repo_worker_deps(
    settings: "Settings", repo: str,
) -> WorkerDeps:
    """Resolve the repo's :class:`WorkerDeps` config via the onboarding API.

    GETs ``/api/v1/onboarding/repos/{repo}`` and returns the response's
    ``worker_deps`` field. ADR-0059 step 2: the runner calls this
    before :func:`treadmill_agent.repo_deps.materialize` so the
    overlay reflects the repo's onboarded extras.

    Returns an empty :class:`WorkerDeps` (no extras) on 404 (repo not
    onboarded — legacy no-deps path), 503 (feature off), or any
    network error. Absence of config must never crash the step; the
    no-overlay path stays in scope so unmigrated repos continue to
    work.
    """
    url = (
        settings.api_url.rstrip("/")
        + f"/api/v1/onboarding/repos/{repo}"
    )
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 503):
            logger.info(
                "repo_deps fetch returned %d for repo=%s; no overlay",
                exc.code, repo,
            )
            return WorkerDeps()
        logger.warning(
            "repo_deps fetch failed (HTTP %d) for repo=%s; no overlay",
            exc.code, repo,
        )
        return WorkerDeps()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "repo_deps fetch failed for repo=%s (%s); no overlay",
            repo, exc,
        )
        return WorkerDeps()
    raw = payload.get("worker_deps") or {}
    try:
        return WorkerDeps.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "repo_deps response malformed for repo=%s (%s); no overlay",
            repo, exc,
        )
        return WorkerDeps()


def resolve_worker_aws_session(settings: "Settings") -> boto3.session.Session:
    """Return the boto3 session the worker uses for every AWS call.

    Per ADR-0019: the local-adapter injects the worker's IAM-User keys
    into the container as env vars before the worker process starts.
    Boto3's default credential chain reads them from the environment;
    this function just returns a region-scoped session.
    """
    return boto3.Session(region_name=settings.aws_region)


def _apply_token_to_gh(token: str) -> None:
    """Hand a token to ``gh`` via stdin and install the git credential helper.

    Shared by both the PAT and App paths. ``input=`` routes the token through
    stdin — the supported channel for ``gh auth login --with-token`` — so it
    never lands in argv (``/proc/<pid>/cmdline``) or the environment.
    """
    result = subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=token.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        raise StartupAuthError(
            f"`gh auth login --with-token` exited {result.returncode}: "
            f"{result.stderr.decode(errors='replace')}"
        )
    # Install the credential helper so plain ``git clone https://github.com/...``
    # URLs route through ``gh``.
    result = subprocess.run(["gh", "auth", "setup-git"], capture_output=True)
    if result.returncode != 0:
        raise StartupAuthError(
            f"`gh auth setup-git` exited {result.returncode}: "
            f"{result.stderr.decode(errors='replace')}"
        )


def bootstrap_github_auth(
    *,
    settings: "Settings",
    aws_session: boto3.session.Session,
) -> None:
    """Legacy PAT path: fetch the PAT from Secrets Manager and hand it to ``gh``."""
    secret_name = settings.github_pat_secret_name
    if not secret_name:
        raise StartupAuthError(
            "repo_mode='github' (pat) requires GITHUB_PAT_SECRET_NAME to be set"
        )
    logger.info("fetching GitHub PAT from Secrets Manager: secret=%s", secret_name)
    secrets = aws_session.client("secretsmanager")
    try:
        resp = secrets.get_secret_value(SecretId=secret_name)
    except Exception as exc:  # noqa: BLE001
        raise StartupAuthError(
            f"failed to fetch GitHub PAT secret {secret_name!r}: {exc}"
        ) from exc
    pat = resp.get("SecretString")
    if not pat:
        raise StartupAuthError(
            f"GitHub PAT secret {secret_name!r} has no SecretString"
        )
    try:
        _apply_token_to_gh(pat)
    finally:
        pat = None  # noqa: F841 - intentional dereference
    logger.info("gh auth bootstrap complete (PAT)")


def bootstrap_github_auth_via_app(
    *,
    settings: "Settings",
    repo: str | None = None,
) -> None:
    """App path: mint a short-lived installation token from the API, hand to ``gh``.

    The worker never holds the App private key — it POSTs to the API's
    ``/api/v1/github/installation-token`` and applies the returned token. Uses
    stdlib ``urllib`` so the worker takes on no new dependency.

    When ``repo`` is ``None`` (the startup home-token bootstrap), POSTs an
    empty body so the API returns a token for the sole installation. When
    ``repo`` is set (``owner/name``), POSTs ``{"repo": repo}`` so the API
    scopes the token to that repo's installation — used by the runner per
    task once ``ctx.repo`` is known.
    """
    url = settings.api_url.rstrip("/") + "/api/v1/github/installation-token"
    body = {"repo": repo} if repo else {}
    if repo:
        logger.info(
            "minting GitHub App installation token via API: %s repo=%s",
            url, repo,
        )
    else:
        logger.info("minting GitHub App installation token via API: %s", url)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        raise StartupAuthError(
            f"failed to mint installation token via {url}: {exc}"
        ) from exc
    token = payload.get("token")
    if not token:
        raise StartupAuthError("installation-token response had no 'token'")
    try:
        _apply_token_to_gh(token)
    finally:
        token = None  # noqa: F841 - intentional dereference
    if repo:
        logger.info(
            "gh auth bootstrap complete (GitHub App installation token, repo=%s)",
            repo,
        )
    else:
        logger.info("gh auth bootstrap complete (GitHub App installation token)")
