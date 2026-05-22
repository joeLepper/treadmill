# Analysis / training corpus

The judgment-role tuning corpus — architect & validator label sets, gold labels,
and the PR diffs they reference, plus the labeling UI — is **not stored in git**.
It is bulky, dev-only data, so it lives in S3 instead and is loaded on demand.

## Load it

```bash
export TREADMILL_CORPUS_S3_URI=s3://<bucket>/docs/analysis/
tools/load-analysis-corpus.sh pull      # → populates docs/analysis/ (gitignored)
```

Push local changes back with `tools/load-analysis-corpus.sh push`.

`docs/analysis/` is gitignored, so a loaded corpus never lands in a commit. Ask
the maintainer for the current `TREADMILL_CORPUS_S3_URI`.
