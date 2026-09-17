# 2026-09-16 — ADR-0119: integration commits use the OPERATOR identity (live-spin fix)

A live spin on treadmill (dummy PR merged by the real integrator) caught that the merge commit
was authored by `treadmill-router <router@treadmill.local>` (git_runner's `_GIT_IDENTITY`, a #415
CI fallback), NOT the operator — GitHub attributed it to the bot, violating ADR-0119's
operator-attribution constraint and tripping its own falsifier. The PUSH was as the operator, but
the COMMIT author is independent.

- `git_runner.py`: `SubprocessGitRunner(..., identity=...)` — the GIT_AUTHOR/COMMITTER env is now
  injectable; default stays the `treadmill-router` fallback (CI / container foils). New
  `identity_env(name, email)` helper.
- `router_integrator.py`: resolves the OPERATOR identity from `ROUTER_INTEGRATOR_GIT_NAME/EMAIL`
  (both required) and passes it to the runner; WARNS at construction if unset (commits would then
  mis-attribute). systemd unit + AGENT.md document setting them to the operator's gh identity with
  a GitHub-verified email.
- Verified in a second live spin: with the identity set, the merge commit is
  `author=committer=Joe Lepper <josephlepper@gmail.com>`, attributed to `joeLepper` on GitHub,
  PR merged, `--no-ff`. Foils: real-git (merge commit uses the injected identity) + integrator
  identity resolution (set → dict; unset/partial → None + warning).
