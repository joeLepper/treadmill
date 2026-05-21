"""S3-backed content-addressed context store (ADR-0050, decision 4).

Blobs are content-addressed by sha256 under ``repo-context/{repo}/{sha}.md``.
The Postgres index that maps repos to keys is a separate follow-up and is
not implemented here.
"""

from __future__ import annotations

import hashlib


class ContextStore:
    def __init__(self, s3_client, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def put_doc(self, repo: str, content: str | bytes) -> str:
        body = content.encode("utf-8") if isinstance(content, str) else content
        sha = hashlib.sha256(body).hexdigest()
        key = f"repo-context/{repo}/{sha}.md"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=body)
        return key

    def presigned_get_url(self, key: str, expires_in: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    def presigned_put_url(self, key: str, expires_in: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
