"""Doc sync helpers for ``treadmill-local docs`` commands (ADR-0054).

Pure, testable functions — callers inject the HTTP callables so tests
can mock entirely without a network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen


def _http_get(url: str) -> tuple[int, bytes]:
    with urlopen(url) as resp:
        return resp.status, resp.read()


def _http_put(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
    req = Request(url, data=body, headers=headers, method="PUT")
    with urlopen(req) as resp:
        return resp.status, resp.read()


def list_docs(
    api_url: str,
    repo: str,
    *,
    get: Callable[[str], tuple[int, bytes]] = _http_get,
) -> list[dict]:
    """GET /api/v1/repos/{repo}/docs — return the doc summaries.

    The API responds with ``{"repo", "docs": [{"doc_path", "version"}]}``;
    we return the ``docs`` list.
    """
    url = f"{api_url.rstrip('/')}/api/v1/repos/{repo}/docs"
    status, body = get(url)
    if status >= 400:
        raise RuntimeError(f"GET {url} returned {status}: {body.decode()}")
    return json.loads(body)["docs"]


def get_doc(
    api_url: str,
    repo: str,
    doc_path: str,
    *,
    get: Callable[[str], tuple[int, bytes]] = _http_get,
) -> str:
    """Fetch single doc content via presigned URL.

    GETs the doc metadata to obtain the presigned URL, then fetches
    and returns the content as a string.
    """
    url = f"{api_url.rstrip('/')}/api/v1/repos/{repo}/docs/{doc_path}"
    status, body = get(url)
    if status >= 400:
        raise RuntimeError(f"GET {url} returned {status}: {body.decode()}")
    meta = json.loads(body)
    _, content = get(meta["url"])
    return content.decode()


def pull(
    api_url: str,
    repo: str,
    dest: Path,
    *,
    get: Callable[[str], tuple[int, bytes]] = _http_get,
) -> list[str]:
    """Download all docs for *repo* into *dest*/<doc_path>.

    Creates parent directories as needed. Returns the list of relative
    paths written.
    """
    docs = list_docs(api_url, repo, get=get)
    written: list[str] = []
    for doc in docs:
        doc_path = doc["doc_path"]
        content = get_doc(api_url, repo, doc_path, get=get)
        out = dest / doc_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        written.append(doc_path)
    return written


def push(
    api_url: str,
    repo: str,
    src: Path,
    *,
    put: Callable[[str, bytes, dict[str, str]], tuple[int, bytes]] = _http_put,
) -> list[tuple[str, int]]:
    """Upload all files under *src* to the docs API.

    PUTs each file to /api/v1/repos/{repo}/docs/{relpath} with
    ``{"content": <text>}``. Returns (doc_path, new_version) pairs.
    Last-write-wins per ADR-0054.
    """
    results: list[tuple[str, int]] = []
    if not src.is_dir():
        return results
    for file in sorted(src.rglob("*")):
        if not file.is_file():
            continue
        relpath = str(file.relative_to(src))
        url = f"{api_url.rstrip('/')}/api/v1/repos/{repo}/docs/{relpath}"
        body = json.dumps({"content": file.read_text()}).encode()
        status, resp_body = put(url, body, {"Content-Type": "application/json"})
        if status >= 400:
            raise RuntimeError(f"PUT {url} returned {status}: {resp_body.decode()}")
        data = json.loads(resp_body)
        results.append((relpath, data.get("version", 0)))
    return results
