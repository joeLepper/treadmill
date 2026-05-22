#!/usr/bin/env bash
# Load (or push) the analysis / model-training corpus.
#
# The corpus (architect/validator label sets + PR diffs used to tune the
# judgment roles) is kept OUT of git — it is bulky and is dev-only data — and
# lives in S3 instead. See docs/analysis-corpus.md.
#
# Usage:
#   TREADMILL_CORPUS_S3_URI=s3://<bucket>/docs/analysis/ tools/load-analysis-corpus.sh [pull|push]
#
#   pull (default)  download the corpus from S3 into docs/analysis/
#   push            upload local docs/analysis/ back to S3
#
# Auth: uses your default AWS credentials; pass AWS_PROFILE=... if needed.
set -euo pipefail

: "${TREADMILL_CORPUS_S3_URI:?Set TREADMILL_CORPUS_S3_URI to the corpus S3 URI, e.g. s3://my-bucket/docs/analysis/}"

mode="${1:-pull}"
root="$(git rev-parse --show-toplevel)"
local_dir="$root/docs/analysis"

case "$mode" in
  pull)
    mkdir -p "$local_dir"
    aws s3 sync "$TREADMILL_CORPUS_S3_URI" "$local_dir"
    echo "Corpus pulled to $local_dir"
    ;;
  push)
    aws s3 sync "$local_dir" "$TREADMILL_CORPUS_S3_URI"
    echo "Corpus pushed to $TREADMILL_CORPUS_S3_URI"
    ;;
  *)
    echo "unknown mode '$mode' (expected pull|push)" >&2
    exit 2
    ;;
esac
