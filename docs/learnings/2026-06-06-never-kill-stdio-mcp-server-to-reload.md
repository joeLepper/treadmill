---
date: 2026-06-06
trigger: surprise
status: captured
related: ADR-0068
---

# Learning: Killing a stdio MCP server breaks the session permanently

## Trigger
Needed to bounce the treadmill-events bun server to load a code fix
(commit 9cc6a0e — adding `pr_merged` to the terminal-status filter).
Sent `kill -TERM <pid>` to the bun process. Claude Code emitted
"treadmill-events disconnected" and did not restart the server. The
session lost the events channel for the rest of its lifetime.

## Observation
`treadmill-events.ts` uses `StdioServerTransport` — it communicates
with Claude Code via stdin/stdout pipes established at launch. Once the
process dies the pipes close; there is no reconnection path for stdio
MCP servers. Claude Code does not auto-restart them.

## Generalization
For stdio MCP servers, a code change can only be loaded by restarting
the Claude session, not by bouncing the server process alone. HTTP/SSE
servers can be bounced independently because the transport survives a
process restart on the same port.

## Proposed rule
Never send `kill` to an stdio MCP server process mid-session to pick
up a code change. Either (a) restart the whole session, or (b) defer
the running server's reload until the next natural session restart.

## Proposed remediation
Add a check in the treadmill-channel-launch README noting that code
changes to treadmill-events.ts only take effect on session restart.
Alternatively, convert the server to SSE transport so in-place reload
is possible.

## Notes
The fix itself (commit 9cc6a0e) is correct and will apply to all
sessions on next restart. Only this session was impacted by the
premature kill.
