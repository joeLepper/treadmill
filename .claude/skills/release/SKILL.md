---
name: release
description: Ship a code change to running agents. Use when a merged change must reach live hosts — to cut a release from main, adopt it by restart, or record a deliberate version pin. Supersedes ADR-0024 (deploy-watcher) and ADR-0069 (self-heal re-exec) as the routine deployment path.
---

# /release — Ship a code change to running agents

A release is the only path from merged code to a running host. There is no shortcut through a working tree, a branch, or a live process.

## When to invoke

- A change has been merged to main and must reach one or more running agents.
- A host must be moved to a specific release (pin or unpin).
- You need to confirm a host is running the code that is on main.

## When NOT to invoke

- The change is not yet merged. Merge first; then invoke this skill.
- You are only checking whether a host is alive (use the health check, not a release).
- You want to test unmerged code locally — that is a local run, not a release.

## Procedure

### 1. Merge to main first

The change must land on `main` before any release step starts. Code that is only on a branch, in a working tree, or in a running process memory is not releasable.

- Open a PR against `main`.
- Wait for CI green and reviewer approval.
- Merge the PR. The merge commit on `main` is the source of truth.

No path exists from a working tree, a branch, or a terminal session into a running agent's runtime. If you find yourself trying to write code directly into a live process, stop. That is hot-loading, and it is prohibited (see step 3).

### 2. Cut a release from main

After the merge commit lands:

1. Check out `main` and pull to the merge commit.
2. Build the release artifact from that commit (Docker image, wheel, or binary — whatever the component uses).
3. Tag the release in the repo (`git tag`) so the artifact is traceable to a commit.
4. Push the tag and the artifact to the appropriate registry.

The release artifact must be built from a clean checkout of the merge commit, not from a working tree with local modifications.

### 3. Adopt the release by restart

A host adopts a new release by stopping its managed processes at a turn boundary and starting them on the new release. The restart is the unit of adoption.

**Mechanism (ADR-0073):** each orchestrator and worker session runs under a `treadmill-channel@<label>.service` systemd-user unit. Restart a host's session with:

```
systemctl --user restart treadmill-channel@<label>.service
```

The unit picks up the new release on the next start because it pulls the current release artifact at launch time, not at build time.

**Turn boundary rule:** never restart a session mid-task. Wait until the session reaches a natural stop (task complete, idle, or explicitly checkpointed) before restarting. A restart at a turn boundary is safe; a restart mid-handler can orphan in-flight work.

**Consequence of this rule:** a restart can never regress the runtime. The process that starts after a restart reads the release on disk at that moment, which is always the most recently adopted release. This eliminates the class of incident where an OOM-triggered restart silently dropped a hot-loaded fix and resumed on stale code (the exec_otp incident that motivated ADR-0096).

### 4. Never hot-load

Hot-loading is prohibited. Banned mechanisms include:

- `importlib.reload(module)` or any variant of runtime module reload
- Erlang-style code hot-load (`code:load_file`, `l(Module)`)
- Monkeypatching a running service (replacing a live function or class attribute in memory)
- Writing a new `.py` file to the import path of a running process and expecting it to be picked up without restart
- Any mechanism that causes a running process to execute code that was not present at its last start

These mechanisms create a split-brain state: the process holds some code from before the change and some from after, with no defined boundary. When the process restarts later for any reason (OOM, crash, host reboot), it loses the hot-loaded fix and resumes on the stale code that was on disk at the prior release.

The only safe path is a release + restart as described in steps 1-3.

### 5. Record and surface a deliberate version pin

A pin — holding a host on a specific release while main has moved forward — is permitted when there is a deliberate reason (a known regression on the newer release, a freeze window, an in-flight experiment). A pin is never silent.

When you pin a host:

1. Record the pin in the host's configuration or a tracked file (not a mental note, not a chat message).
2. Note the pinned release identifier, the reason, and an expiry condition or owner.
3. Surface the pin where the operator will see it: add a line to the session's relay channel or the relevant incident record.
4. Remove the pin explicitly when the reason is resolved. A pin that is never removed becomes invisible technical debt.

An unrecorded pin is a protocol violation, not a judgment call. The operator cannot manage a release fleet if pins are invisible.
