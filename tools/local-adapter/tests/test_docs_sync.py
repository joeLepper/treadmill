"""Unit tests for treadmill_local.docs_sync — no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from treadmill_local.docs_sync import get_doc, list_docs, pull, push


# ── helpers ──────────────────────────────────────────────────────────────────


def make_get(*responses: tuple[int, bytes]):
    """Fake GET callable that returns *responses* in order."""
    resp_iter = iter(responses)
    calls: list[str] = []

    def _get(url: str) -> tuple[int, bytes]:
        calls.append(url)
        return next(resp_iter)

    _get.calls = calls  # type: ignore[attr-defined]
    return _get


def make_put(*responses: tuple[int, bytes]):
    """Fake PUT callable that returns *responses* in order."""
    resp_iter = iter(responses)
    calls: list[tuple[str, bytes, dict]] = []

    def _put(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
        calls.append((url, body, headers))
        return next(resp_iter)

    _put.calls = calls  # type: ignore[attr-defined]
    return _put


def _list(paths: dict[str, int]) -> bytes:
    """Encode a LIST response: {repo, docs:[{doc_path, version}]}."""
    return json.dumps(
        {"repo": "owner/repo",
         "docs": [{"doc_path": p, "version": v} for p, v in paths.items()]}
    ).encode()


def _meta(doc_path: str, url: str) -> bytes:
    """Encode a GET doc-metadata response: {repo, doc_path, version, url}."""
    return json.dumps(
        {"repo": "owner/repo", "doc_path": doc_path, "version": 1, "url": url}
    ).encode()


# ── list_docs ─────────────────────────────────────────────────────────────────


def test_list_docs_success():
    # The API returns a {repo, docs:[{doc_path, version}]} envelope.
    payload = {"repo": "owner/repo", "docs": [
        {"doc_path": "adrs/0001.md", "version": 1},
        {"doc_path": "plans/x.md", "version": 2},
    ]}
    get = make_get((200, json.dumps(payload).encode()))
    result = list_docs("http://localhost:8088", "owner/repo", get=get)
    assert result == payload["docs"]
    assert get.calls[0] == "http://localhost:8088/api/v1/repos/owner/repo/docs"


def test_list_docs_empty():
    get = make_get((200, b'{"repo": "owner/repo", "docs": []}'))
    result = list_docs("http://localhost:8088", "owner/repo", get=get)
    assert result == []


def test_list_docs_trailing_slash_stripped():
    get = make_get((200, b'{"repo": "owner/repo", "docs": []}'))
    list_docs("http://localhost:8088/", "owner/repo", get=get)
    assert get.calls[0] == "http://localhost:8088/api/v1/repos/owner/repo/docs"


# ── get_doc ───────────────────────────────────────────────────────────────────


def test_get_doc_single_path():
    meta = {"path": "AGENT.md", "url": "https://s3.example.com/agent"}
    get = make_get(
        (200, json.dumps(meta).encode()),
        (200, b"# Agent\nContent here"),
    )
    content = get_doc("http://localhost:8088", "owner/repo", "AGENT.md", get=get)
    assert content == "# Agent\nContent here"
    assert get.calls[0].endswith("/docs/AGENT.md")
    assert get.calls[1] == "https://s3.example.com/agent"


def test_get_doc_nested_path():
    meta = {"path": "docs/guide.md", "url": "https://s3.example.com/guide"}
    get = make_get(
        (200, json.dumps(meta).encode()),
        (200, b"Guide content"),
    )
    content = get_doc("http://localhost:8088", "owner/repo", "docs/guide.md", get=get)
    assert content == "Guide content"
    assert get.calls[0].endswith("/docs/docs/guide.md")


# ── pull ──────────────────────────────────────────────────────────────────────


def test_pull_creates_files(tmp_path: Path):
    # list → {repo, docs:[{doc_path, version}]}; then get_doc 2-hop per doc.
    get = make_get(
        (200, _list({"AGENT.md": 1})),
        (200, _meta("AGENT.md", "https://s3.example.com/agent")),
        (200, b"Agent content"),
    )
    written = pull("http://localhost:8088", "owner/repo", tmp_path, get=get)
    assert written == ["AGENT.md"]
    assert (tmp_path / "AGENT.md").read_text() == "Agent content"


def test_pull_creates_parent_dirs(tmp_path: Path):
    get = make_get(
        (200, _list({"adrs/nested/guide.md": 1})),
        (200, _meta("adrs/nested/guide.md", "https://s3.example.com/guide")),
        (200, b"Guide"),
    )
    written = pull("http://localhost:8088", "owner/repo", tmp_path, get=get)
    assert written == ["adrs/nested/guide.md"]
    assert (tmp_path / "adrs" / "nested" / "guide.md").exists()


def test_pull_returns_all_paths(tmp_path: Path):
    get = make_get(
        (200, _list({"AGENT.md": 1, "README.md": 1})),
        (200, _meta("AGENT.md", "https://s3.example.com/agent")),
        (200, b"Agent content"),
        (200, _meta("README.md", "https://s3.example.com/readme")),
        (200, b"Readme content"),
    )
    written = pull("http://localhost:8088", "owner/repo", tmp_path, get=get)
    assert set(written) == {"AGENT.md", "README.md"}


def test_pull_content_written_correctly(tmp_path: Path):
    get = make_get(
        (200, _list({"notes.md": 1})),
        (200, _meta("notes.md", "https://s3.example.com/notes")),
        (200, b"Hello world"),
    )
    pull("http://localhost:8088", "owner/repo", tmp_path, get=get)
    assert (tmp_path / "notes.md").read_text() == "Hello world"


# ── push ──────────────────────────────────────────────────────────────────────


def test_push_uploads_files(tmp_path: Path):
    (tmp_path / "AGENT.md").write_text("# Agent")
    put = make_put((200, json.dumps({"version": 1}).encode()))
    results = push("http://localhost:8088", "owner/repo", tmp_path, put=put)
    assert results == [("AGENT.md", 1)]
    url, body, _ = put.calls[0]
    assert url == "http://localhost:8088/api/v1/repos/owner/repo/docs/AGENT.md"
    assert json.loads(body) == {"content": "# Agent"}


def test_push_multiple_files(tmp_path: Path):
    (tmp_path / "a.md").write_text("A")
    (tmp_path / "b.md").write_text("B")
    put = make_put(
        (200, json.dumps({"version": 1}).encode()),
        (200, json.dumps({"version": 2}).encode()),
    )
    results = push("http://localhost:8088", "owner/repo", tmp_path, put=put)
    assert len(results) == 2
    paths = [r[0] for r in results]
    assert "a.md" in paths
    assert "b.md" in paths


def test_push_skips_directory_entries(tmp_path: Path):
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "file.md").write_text("content")
    put = make_put((200, json.dumps({"version": 1}).encode()))
    results = push("http://localhost:8088", "owner/repo", tmp_path, put=put)
    # Only the file inside the subdir is uploaded, not the directory itself
    assert len(results) == 1
    assert "subdir" in results[0][0]
    assert len(put.calls) == 1


def test_push_version_extracted(tmp_path: Path):
    (tmp_path / "doc.md").write_text("content")
    put = make_put((200, json.dumps({"version": 42}).encode()))
    results = push("http://localhost:8088", "owner/repo", tmp_path, put=put)
    assert results[0][1] == 42
