- **Recent-changes entries became per-PR fragment files (task
  986c5cf6)**: every AGENT.md `## Recent changes` section now points at
  a sibling `agent-changes/` directory; new entries are one-file-per-PR
  (`YYYY-MM-DD-<task-or-pr-slug>.md`, this file is the first), killing
  the prepend conflict factory that stacked three same-day rework
  cascades on 2026-06-12. Authoring flow in `docs/agent-md-schema.md`;
  decision record incl. why-not `merge=union` / dated headings in
  `docs/learnings/2026-06-12-agent-md-recent-changes-is-a-conflict-factory.md`;
  enforced by `tools/dev-hooks/tests/test_agent_md_conventions.py`.
