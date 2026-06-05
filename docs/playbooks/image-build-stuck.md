# Image build stuck — image build fails repeatedly

**Related:** ADR-0018 (autoscaler), ADR-0075 (operator obligations), 2026-06-05 learning (autoscaler-ignores-no-build-and-stalls-silently)

## Symptom

The autoscaler or image-build pipeline fails repeatedly to build a Docker image. The error appears in logs every N seconds (typically every 5 seconds if it's an autoscaler tick), but the system doesn't escalate—it just keeps retrying silently. Tasks are dispatched but workers never spawn because the image is unavailable. This wedges the queue and creates the appearance of "tasks stuck executing" when in fact "no worker ever started."

Example error:

```
RuntimeError: docker build failed for treadmill-dashboard:dev; refusing to start containers with a stale image. Re-run with `--no-build` to bypass.
```

## How to identify whether the fallback is engaged

First, check if the `--no-build` escape hatch is currently active:

**Dev-local:**

```bash
# Check if autoscaler is running with --no-build
ps aux | grep treadmill-autoscaler | grep -i "no-build"

# If it's running with --no-build, you'll see the flag in the process args
# Or check the launcher config:
grep -r "no-build" .treadmill-local/config/
```

**Cloud deployment:**

```bash
# Check the autoscaler pod's command args
kubectl get pod <autoscaler-pod> -o yaml | grep -A 5 "args:"
# Or check environment variables:
kubectl exec <autoscaler-pod> -- env | grep NO_BUILD
```

If `--no-build` is active, the autoscaler is skipping image builds and using stale images. This is a temporary workaround—the fallback is engaged and the system is running degraded. Proceed to the root cause checklist to identify why the image build is failing.

## Root cause checklist

- [ ] **Syntax error in application code**: A TypeScript, Python, or Go error in the codebase prevents the image from building. The error appears in the Docker build output but not in static CI checks (e.g., tsc error in TS code not caught by CI linting).
  - Check: Docker build logs in `.treadmill-local/autoscaler.log` or cloud build logs for compilation errors.

- [ ] **Missing or unavailable dependency**: A package or external resource the image build needs is no longer available (npm package yanked, git commit deleted, external URL down).
  - Check: Docker build logs for `not found`, `404`, or `ENOTFOUND` errors.

- [ ] **Base image unavailable**: The `FROM` image in the Dockerfile is no longer available in the registry or its tag was re-assigned.
  - Check: `docker pull <base-image>` to verify it's still accessible.

- [ ] **Filesystem or resource exhaustion**: The worker machine running the build ran out of disk space or memory.
  - Check: `df -h /` and `free -m` on the build machine; check Docker daemon logs.

- [ ] **Permission or credential issue**: The build step needs to pull from a private registry or authenticate to a service, but credentials are missing or expired.
  - Check: Docker login status (`docker login <registry>` or check `~/.docker/config.json`).

## Commands

**Inspect autoscaler logs (dev-local):**

```bash
tail -100 .treadmill-local/autoscaler.log | grep -A 5 "docker build failed\|error\|Error"
```

Look for the actual build error (compilation, missing dependency, etc.) before the "refusing to start containers" message.

**Run the image build manually to reproduce:**

```bash
# Identify the Dockerfile and image name from autoscaler.log
docker build -t treadmill-dashboard:dev -f treadmill/dashboard/Dockerfile .
```

This will show the exact error without the autoscaler's error-handling wrapper.

**Check git status and recent commits:**

```bash
git status
git log --oneline -10
# Did a recent commit introduce the error?
```

**If it's a TypeScript error, run tsc directly:**

```bash
cd treadmill/dashboard
npx tsc -b
# Or: npm run build
```

**If it's a Python error, run the build step:**

```bash
# Check if there's a build script or setup.py
python -m py_compile treadmill/*.py
# Or: python setup.py check
```

**If it's a missing dependency, check the registry:**

```bash
# For npm
npm view <package-name> version  # Does the package exist?

# For pip
pip index versions <package-name>

# For Docker base image
docker pull <base-image>:<tag>
```

**Check filesystem and resource availability:**

```bash
df -h /  # Disk space
free -m  # Memory
docker system df  # Docker image/container space
```

## Durable fix

**Short term (unblock the queue):**

**Decision: Roll back vs. fix forward**

First, determine whether to roll back the problematic commit or fix forward:

- **Roll back if**: The error was introduced in a recent commit (within the last few hours) and the fix requires significant changes. Reverting is faster.
- **Fix forward if**: The error is in old code and you have a quick fix, or rolling back would lose recent work from other developers.

**To identify the problematic commit:**

```bash
git log --oneline -20
# Look for a recent commit that touched the file with the error (usually in the Dockerfile, a build script, or application code)

# Or search for commits that touched the image build:
git log --oneline -- Dockerfile treadmill/dashboard/package.json treadmill/dashboard/tsconfig.json
```

**If rolling back:**

```bash
# Option 1: Revert the problematic commit (preferred, creates a new commit)
git revert <commit-hash>
git push origin main

# Option 2: Reset to the last known-good commit (if you're absolutely sure it's safe)
git reset --hard <known-good-commit>
git push --force origin main  # Use with caution; only if you're certain no one is building on that commit
```

**If fixing forward:**

1. Fix the code error directly:
   - If it's a code error (TS syntax, import error): Fix the bug in the code, commit, and push.
   - If it's a missing dependency: Add it to the dependency manifest (package.json, go.mod, etc.), commit, and push.
   - If it's a resource issue: Free up space (`docker system prune -a`, extend disk), or move the build to a machine with more resources.

2. **Restart the autoscaler once the fix is in place**:
   ```bash
   pkill treadmill-autoscaler
   # For dev-local, the launcher will restart it automatically
   # For cloud, redeploy the autoscaler pod/container
   ```

3. **Verify the image builds**:
   ```bash
   docker build -t treadmill-dashboard:dev -f treadmill/dashboard/Dockerfile .
   ```

4. **Once the image is built, workers should spawn within one tick (~5 seconds)** and the queue should start draining.

**Important: Don't use `--no-build` as a permanent workaround.** The `--no-build` flag tells the autoscaler to skip image building. This unblocks the queue temporarily but runs workers against stale images. Once you've fixed the root cause, always rebuild. Never leave the system running workers from stale code.

**Long term (prevent recurrence):**

- **Enforce build validation in CI**: The dashboard (or any image built at runtime) should have a CI check that runs the exact same `tsc -b && docker build` commands that the Dockerfile runs. This catches code errors before they land on main.
  - Example: Add a GitHub Actions step that builds the image on every PR.

- **Use `--no-build` only for local development**: The escape hatch `--no-build` is meant for developers iterating on non-image code. Ensure this flag is:
  - Only used by developers during local work (not in CI or production).
  - Documented clearly: "Use `--no-build` only when iterating on code you know builds, to skip image rebuilds. Always rebuild before merging."

- **Make image-build wedges visible**: The autoscaler should not fail silently for N minutes. Implement escalation:
  - After M consecutive failed ticks (e.g., M=12, ≈1 min), write a `system_event` of kind `autoscaler_wedged` that the dashboard surfaces and alerts the operator-relay (ADR-0071 significant set).
  - Or, update `treadmill task list` to show `stalled (no worker)` for tasks whose workflow_run_steps have been `pending` for >5 min despite visible queue depth. This makes queue starvation visible on the surface operators watch.

- **Add a build-image health check**: Before spawning workers, verify that the target image exists locally and is buildable:
  ```python
  def _ensure_images_built():
    try:
      docker_build(...)
    except BuildError as e:
      escalate_to_operator(f"Image build failed: {e}")
      raise
  ```

  Do not silently retry; escalate so an operator is aware.

- **Document the `--no-build` workaround boundaries**: In the autoscaler code and user docs, clearly state:
  - When `--no-build` is safe (code changes that don't affect the image, e.g., SQL scripts, configuration).
  - When it's unsafe (application code changes, dependency changes, Dockerfile changes).

- **Plumb `--no-build` through to subprocesses**: If a flag like `--no-build` is meant to apply to the whole system, it must be passed through all subprocesses. Verify that `treadmill-local up --no-build` actually disables image building in the autoscaler subprocess, not just the parent process. See ADR-0018 for the autoscaler's architecture.
