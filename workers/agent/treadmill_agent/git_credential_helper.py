"""On-demand git credential helper for the worker.

Installed as a global git credential helper for ``https://github.com`` by
:func:`treadmill_agent.startup_auth._install_git_credential_helper`. Each
invocation mints a fresh short-lived GitHub App installation token via the
API's ``/api/v1/github/installation-token`` endpoint and hands it to git via
the credential protocol.

The startup-time ``gh auth setup-git`` flow installs a credential helper that
caches a single token for the lifetime of the worker — long-running builds
that outlive that token's expiry then 401 on push. This helper sidesteps
that by minting per-operation, so the token git sees is always fresh.

Contract (git's credential helper protocol):

  * argv[1] is the action (``get``/``store``/``erase``); only ``get`` does
    anything. ``store``/``erase`` exit 0 silently (there's nothing to cache).
  * stdin carries ``key=value`` lines until a blank line.
  * On a ``get`` for ``host=github.com``, stdout is two lines plus a blank:
    ``username=x-access-token``, ``password=<token>``.
  * Any failure logs to stderr and exits 0 with empty stdout — a credential
    helper that crashes would break ``git clone`` / ``git push``. Git
    treats no output as "this helper had no opinion" and moves on to the
    next helper / fails the operation with its own clean error.

Security:

  * The token is read from the API response and written to stdout only; it
    never appears in argv (``/proc/<pid>/cmdline``) or in logs.
  * Hosts other than ``github.com`` exit 0 with no output so this helper
    can be unconditionally registered without exfiltrating credentials for
    other hosts.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _read_credential_attrs(stream) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Parse git's credential-protocol ``key=value`` lines until EOF/blank."""
    attrs: dict[str, str] = {}
    for raw in stream:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            break
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        attrs[key] = value
    return attrs


def _derive_repo(path: str | None) -> str | None:
    """Turn a credential ``path`` like ``owner/name.git`` into ``owner/name``.

    Git fills the ``path`` attribute from the URL when
    ``credential.useHttpPath`` is set (we set it in
    ``_install_git_credential_helper`` so the API can scope the minted
    token to the right installation). Only the first two path segments
    matter — git can include trailing segments like ``info/refs`` on
    fetch, which are stripped here. Returns ``None`` when the path is
    missing or doesn't have the ``owner/name`` shape — the helper falls
    back to the home-installation mint in that case.
    """
    if not path:
        return None
    parts = path.strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not name:
        return None
    return f"{owner}/{name}"


def _mint_token(api_url: str, repo: str | None) -> str:
    """POST to the API to mint an installation token. Returns the token string."""
    url = api_url.rstrip("/") + "/api/v1/github/installation-token"
    body = {"repo": repo} if repo else {}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    token = payload.get("token")
    if not token:
        raise RuntimeError("installation-token response had no 'token'")
    return token


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for ``python -m treadmill_agent.git_credential_helper``.

    Always returns 0 — see the module docstring for why a credential helper
    must never crash a git operation. The action is taken from
    ``argv[1]``; only ``get`` produces output.
    """
    argv = argv if argv is not None else sys.argv
    action = argv[1] if len(argv) > 1 else ""
    if action != "get":
        # ``store`` and ``erase`` are no-ops — nothing is cached.
        return 0

    try:
        attrs = _read_credential_attrs(sys.stdin)
        host = attrs.get("host", "")
        if host != "github.com":
            return 0
        repo = _derive_repo(attrs.get("path"))
        api_url = os.environ.get("TREADMILL_API_URL", "http://treadmill-api:8088")
        token = _mint_token(api_url, repo)
        # The credential protocol expects ``key=value`` lines terminated by
        # a blank line. ``write`` (not ``print``) so we never accidentally
        # add anything else to stdout.
        sys.stdout.write("username=x-access-token\n")
        sys.stdout.write(f"password={token}\n")
        sys.stdout.write("\n")
        sys.stdout.flush()
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        # Log a sanitized reason (no token can be in scope yet on the error
        # paths above) and let git fall through to its own auth failure.
        sys.stderr.write(
            f"treadmill git-credential-helper failed: {type(exc).__name__}: {exc}\n"
        )
    except Exception as exc:  # noqa: BLE001 - never crash a git operation
        sys.stderr.write(
            f"treadmill git-credential-helper unexpected error: "
            f"{type(exc).__name__}: {exc}\n"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
