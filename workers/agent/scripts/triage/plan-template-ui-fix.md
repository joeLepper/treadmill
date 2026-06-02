sequence_of_work:
  - id: triage-fix-<FINDING_ID_SHORT>
    title: "ui-fix from triage finding <FINDING_ID_SHORT> — <SHORT_OBSERVATION>"
    workflow: wf-author
    intent: |
      STUDY:
        - <EVIDENCE_POINTER>

      BUILD:
        <PROPOSED_RESOLUTION>

      TESTS:
        - Author a new vitest test file alongside the modified
          component at <TEST_FILE_PATH> that renders the relevant
          component(s) via `@testing-library/react` and asserts
          the bug no longer reproduces. The assertion mirrors what
          the triage finding's `proposed_resolution` says should
          be true after the fix.

      DOC:
        - Update the touched component's AGENT.md
          Recent-changes entry citing this triage
          finding's <FINDING_ID_SHORT>.
    scope:
      files:
        - <PROPOSED_RESOLUTION_FILES>
        - <TEST_FILE_PATH>
        - <COMPONENT_AGENT_MD>
      services_affected:
        - services/dashboard
      out_of_scope:
        - Unrelated dashboard cleanups
        - Changes to the triage role prompt itself
    validation:
      - kind: deterministic
        description: "The bug-regression vitest test exists, contains an assertion derived from the finding's proposed_resolution, and references the triage finding ID."
        script: |
          set -euo pipefail
          test -f <TEST_FILE_PATH>
          grep -q "<FINDING_ID_SHORT>" <TEST_FILE_PATH>
          grep -q "<VITEST_ASSERTION_SIGNATURE>" <TEST_FILE_PATH>
        severity: blocking
        timeout_seconds: 30
