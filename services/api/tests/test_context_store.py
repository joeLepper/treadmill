"""Unit tests for treadmill_api.context_store — S3-backed blob store."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

from treadmill_api.context_store import ContextStore


def test_put_doc_returns_content_addressed_key_and_calls_s3():
    s3 = MagicMock()
    store = ContextStore(s3, bucket="ctx-bucket")

    key = store.put_doc("o/r", "hello")

    expected_sha = hashlib.sha256(b"hello").hexdigest()
    assert key.startswith("repo-context/o/r/")
    assert expected_sha in key
    s3.put_object.assert_called_once_with(
        Bucket="ctx-bucket", Key=key, Body=b"hello"
    )


def test_put_doc_is_idempotent_for_identical_content():
    s3 = MagicMock()
    store = ContextStore(s3, bucket="ctx-bucket")

    key1 = store.put_doc("o/r", "hello")
    key2 = store.put_doc("o/r", "hello")

    assert key1 == key2


def test_presigned_get_url_delegates_to_s3_client():
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://signed.example/get"
    store = ContextStore(s3, bucket="ctx-bucket")

    url = store.presigned_get_url("k")

    assert url == "https://signed.example/get"
    s3.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "ctx-bucket", "Key": "k"},
        ExpiresIn=3600,
    )
