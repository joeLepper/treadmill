# treadmill_local autoscaler image-build fallback

## Image Build Fallback Behavior

The autoscaler implements a multi-tier fallback strategy for handling consecutive image-build failures, avoiding cascading worker spawn failures when the build substrate is temporarily unavailable.

### Environment Variable

**`TREADMILL_AUTOSCALER_BUILD_IMAGES`** (default: `"true"`)

Propagates the parent `--no-build` flag to the autoscaler subprocess. When set to `"false"`, `"0"`, or `"no"`, the autoscaler skips image builds entirely and uses the last-known-good image. This ensures that `treadmill-local up --no-build` skips rebuilds in both the parent and the autoscaler child process.

The `main()` function reads this env var and constructs `LocalRuntime(build_images=...)` accordingly.

### Consecutive Failure Tracking (K=12)

The autoscaler tracks consecutive `_ensure_images_built` RuntimeErrors in a `_consecutive_build_failures` counter:

- **Increment:** Counter increments when `start_worker_once()` raises a RuntimeError containing `"docker build"` (case-insensitive)
- **Only during normal mode:** Counter only increments when NOT already in fallback mode
- **Reset on success:** Counter resets to 0 when a worker starts successfully (no exception raised)

### Fallback Activation (K=12 consecutive failures)

After **K=12 consecutive failures** (~1 minute at 5-second ticks):

1. Set `_fallback_ticks = 1` to enter fallback mode
2. Reset `_consecutive_build_failures` to 0 (the threshold was met once; further tracking happens in fallback mode)
3. Log: `"image build failed 12 times; will use fallback (last-known-good image)"`

During fallback ticks:

- Worker spawns call `start_worker_no_build_fn()` instead of `start_worker_fn()`
- This temporarily disables image builds: `runtime.build_images = False` for the duration of the spawn call
- Workers run against the last-known-good image rather than attempting a fresh build

### Escalation (F=3 fallback ticks)

After **F=3 consecutive fallback ticks** without recovery:

1. Set `_image_build_broken_reported = True` (one-time flag to prevent duplicate reports)
2. Log: `"image_build_broken: escalating after 3+ fallback ticks"`
3. Emit `{"image_build_broken": true}` via the `heartbeat_fn` callback

The API server will consume this heartbeat field and emit a `system.image_build_broken` event (phase 2, future work). This allows the operator to observe that the local build substrate has been unavailable for more than 3 ticks (~15 seconds).

## Test Coverage

`tools/local-adapter/tests/test_autoscaler_fallback.py` covers:

1. **`test_env_var_disables_build`:** Verifies that `TREADMILL_AUTOSCALER_BUILD_IMAGES` env var is correctly parsed to enable/disable builds
2. **`test_k_consecutive_failures_triggers_fallback`:** Verifies that after K=12 consecutive build failures, fallback mode is triggered and the next tick calls `start_worker_no_build_fn()`
3. **`test_f_fallback_ticks_marks_image_build_broken`:** Verifies that after F=3 fallback ticks, an `image_build_broken` heartbeat is emitted
4. **`test_successful_build_resets_counter`:** Verifies that a successful build (no exception) resets the consecutive failure counter to 0

## Related

- **Autoscaler:** `autoscaler.py` — control loop and fallback logic; `main()` reads env var and wires callables
- **Runtime:** `runtime.py` — `LocalRuntime(build_images=...)` flag; `start_worker_once()` call site that may raise build errors
- **Heartbeat:** Callback passed as `heartbeat_fn` to Autoscaler; API server (phase 2) will emit events when field flips to true
