/**
 * Regression test for triage finding `0b1dbe45`: a task whose
 * `derived_status` is a composite string like `pr_opened (wf-conflict: failed)`
 * was falling through every explicit branch in `deriveLifecycleIdx` and
 * returning 0 (REGISTERED), so the stepper highlighted step 01 amber for
 * a task that clearly had an open PR and an active (failed) workflow run.
 * The operator's mental model is that anything with an open PR is at least
 * in the EXECUTING phase, so we map the composite string to lifecycle
 * index 1.
 */
import { describe, expect, it } from 'vitest';

import { deriveLifecycleIdx } from './Lifecycle';

describe('deriveLifecycleIdx', () => {
  it('maps composite pr_opened (wf-conflict: failed) to EXECUTING (1)', () => {
    expect(deriveLifecycleIdx('pr_opened (wf-conflict: failed)')).toBe(1);
  });

  it('maps bare pr_opened to EXECUTING (1)', () => {
    expect(deriveLifecycleIdx('pr_opened')).toBe(1);
  });

  it('maps other (wf-...) composite strings to EXECUTING (1)', () => {
    expect(deriveLifecycleIdx('executing (wf-build: running)')).toBe(1);
  });

  it('still defaults unknown statuses to REGISTERED (0)', () => {
    expect(deriveLifecycleIdx('something_unmapped')).toBe(0);
    expect(deriveLifecycleIdx(null)).toBe(0);
    expect(deriveLifecycleIdx(undefined)).toBe(0);
  });

  it('preserves existing mappings', () => {
    expect(deriveLifecycleIdx('validated')).toBe(4);
    expect(deriveLifecycleIdx('merged')).toBe(3);
    expect(deriveLifecycleIdx('awaiting_review')).toBe(2);
    expect(deriveLifecycleIdx('blocked-on-review')).toBe(1);
    expect(deriveLifecycleIdx('failed')).toBe(1);
  });
});
