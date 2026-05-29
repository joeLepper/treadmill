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
        - The Playwright validation script below must
          pass against the live dashboard at
          http://treadmill-dashboard:80/ after the fix
          lands.

      DOC:
        - Update the touched component's AGENT.md
          Recent-changes entry citing this triage
          finding's <FINDING_ID_SHORT>.
    scope:
      files:
        - <PROPOSED_RESOLUTION_FILES>
        - <COMPONENT_AGENT_MD>
      services_affected:
        - services/dashboard
      out_of_scope:
        - Unrelated dashboard cleanups
        - Changes to the triage role prompt itself
    validation:
      - kind: deterministic
        description: "Playwright asserts <FINDING_ID_SHORT> no longer reproduces against http://treadmill-dashboard:80/."
        script: |
          node -e '
            const { chromium } = require("playwright");
            (async () => {
              const browser = await chromium.launch({ headless: true });
              const page = await browser.newPage();
              await page.goto("http://treadmill-dashboard:80/<TARGET_PATH>", { waitUntil: "networkidle" });
              <PLAYWRIGHT_ASSERTION_DERIVED_FROM_PROPOSED_RESOLUTION>
              await browser.close();
            })().catch(e => { console.error(e); process.exit(1); });
          '
        severity: blocking
        timeout_seconds: 120
