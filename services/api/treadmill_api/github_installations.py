"""Onboarded-repo installation registry (multi-org on-ramp).

Tracks which repos Treadmill is onboarded onto and their GitHub App
installation ids. Distinct from the token cache in ``github_app.py``: that
cache answers "what's a fresh installation token", this registry answers
"is this repo onboarded, and what is its installation id".

TODO(ADR-0049): persist this registry to the database so onboarding survives
process restarts. In-memory for now to keep the on-ramp small.
"""

from __future__ import annotations

import httpx

from treadmill_api.github_app import resolve_installation_id


class InstallationRegistry:
    """In-memory map of onboarded ``owner/name`` repos to installation ids."""

    def __init__(self) -> None:
        self._installations: dict[str, int] = {}

    def record(self, repo: str, installation_id: int) -> None:
        self._installations[repo] = installation_id

    def is_onboarded(self, repo: str) -> bool:
        return repo in self._installations

    def known(self) -> dict[str, int]:
        return dict(self._installations)

    async def resolve(
        self,
        client: httpx.AsyncClient,
        *,
        app_id: str,
        private_key_pem: str,
        repo: str,
    ) -> int:
        """Return the recorded installation id, resolving + recording on miss."""
        cached = self._installations.get(repo)
        if cached is not None:
            return cached
        installation_id = await resolve_installation_id(
            client,
            app_id=app_id,
            private_key_pem=private_key_pem,
            repo=repo,
        )
        self._installations[repo] = installation_id
        return installation_id
