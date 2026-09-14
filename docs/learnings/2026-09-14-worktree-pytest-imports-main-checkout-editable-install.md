---
date: 2026-09-14
trigger: surprise
status: captured
related: ADR-0111
---

# Learning: pytest in a git worktree imports the MAIN checkout, not the worktree source

## Trigger
During the ADR-0112 cross-model review, Ernie ran a red-then-green foil on a CLI change
inside a throwaway git worktree: he mutated `team.py` in the worktree and ran `pytest`,
expecting a test to redden — it came back GREEN. He then confirmed `python -c "import
treadmill_cli"` resolved to `/home/joe/treadmill/cli/treadmill_cli` — the MAIN checkout,
not the worktree. He nearly shipped a false clearance.

## Observation
The venv holds an EDITABLE install of `treadmill_cli` (a `.pth` pointing at
`/home/joe/treadmill/cli`). A `pytest` run from inside a worktree still imports the
package from that main-checkout path, so a foil that edits the WORKTREE's source tests
the UNMUTATED main checkout. The foil passes regardless of the mutation — a vacuous
green. Template foils that read a `.tmpl` by relative path were unaffected; only
import-based (CLI/Python-package) foils were.

## Generalization
Any editable-installed package makes worktree-based verification of that package's
Python source unreliable: the import resolves to the install target (main checkout), not
the worktree. A "ran the suite at commit X in a throwaway worktree" claim actually tests
whatever SHA the MAIN checkout is on at that moment, not X.

## Proposed rule
When verifying editable-installed Python code (e.g. `treadmill_cli`) from a git worktree,
force the worktree source with `PYTHONPATH=<worktree>/cli pytest …` (it shadows the
`.pth` editable install — verified), or install from the worktree. Do NOT trust a
worktree foil's green without it. Template/relative-path foils are exempt.

## Proposed remediation
none (process rule for reviewers). A reviewer's co-sign on a worktree-run CLI foil
should state that PYTHONPATH was set (or the package installed from the worktree), else
the green is meaningless.

## Notes
Caught by Ernie mid-verification on ADR-0112; the final 325-pass for that ADR was on the
correct source because 958e15a was the main HEAD at check time. Reported fleet-wide.
