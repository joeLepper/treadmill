/**
 * Tests for the ADR-0089 wake-class filter (wake-filter.ts).
 *
 * Coverage pinned by the plan (2026-06-11-adr-0089-token-economics-
 * implementation.md, task wake-filter):
 *   * role defaults — incl. the two ENUMERATED escalation-class actions
 *     (task.evaluator_timeout, task.rework_exhausted) waking;
 *   * glob matching semantics;
 *   * digest accumulate / reset;
 *   * a suppressed-only stream produces a digest wake within
 *     max-suppression-age (+ one poll period — the poll period is the
 *     caller's timer; here we drive the injected clock past the age);
 *   * the wake ⊇ relay superset WARN fires on a violating pair.
 */
import { describe, expect, test } from 'bun:test'

import {
  COORDINATOR_DEFAULT_WAKE_ACTIONS,
  EVALUATOR_DEFAULT_WAKE_ACTIONS,
  ORCHESTRATOR_DEFAULT_WAKE_ACTIONS,
  RELAY_SIGNIFICANT_ACTIONS,
  WakeGate,
  globToRegExp,
  parseWakeActions,
  wakeSetViolations,
} from './wake-filter.ts'

// ── glob matching ───────────────────────────────────────────────────────────

describe('globToRegExp', () => {
  test('* spans any run of characters', () => {
    expect(globToRegExp('task.*_verdict').test('task.evaluator_verdict')).toBe(true)
    expect(globToRegExp('task.escalat*').test('task.escalated_to_operator')).toBe(true)
    expect(globToRegExp('task.escalat*').test('task.escalation_closed')).toBe(true)
    expect(globToRegExp('datamigration.*').test('datamigration.first_success')).toBe(true)
  })

  test('match is anchored — no substring hits', () => {
    expect(globToRegExp('task.escalat*').test('xtask.escalated')).toBe(false)
    expect(globToRegExp('task.*_verdict').test('task.evaluator_verdict_extra')).toBe(false)
  })

  test('non-* characters are literal: the dot separator does not match any char', () => {
    expect(globToRegExp('github.pr_merged').test('github.pr_merged')).toBe(true)
    expect(globToRegExp('github.pr_merged').test('githubXpr_merged')).toBe(false)
  })

  test('no glob means exact match only', () => {
    expect(globToRegExp('task.cancelled').test('task.cancelled')).toBe(true)
    expect(globToRegExp('task.cancelled').test('task.cancelled_late')).toBe(false)
  })
})

// ── role defaults / env parsing ─────────────────────────────────────────────

describe('parseWakeActions', () => {
  test('explicit env wins over role default, whitespace-tolerant', () => {
    expect(parseWakeActions(' github.pr_merged , task.* ', 'orchestrator')).toEqual([
      'github.pr_merged',
      'task.*',
    ])
  })

  test('orchestrator role defaults to the ADR-0089 set when env unset', () => {
    expect(parseWakeActions(undefined, 'orchestrator')).toEqual([
      ...ORCHESTRATOR_DEFAULT_WAKE_ACTIONS,
    ])
    expect(parseWakeActions('', 'Orchestrator')).toEqual([
      ...ORCHESTRATOR_DEFAULT_WAKE_ACTIONS,
    ])
  })

  test('worker / unset roles stay unfiltered; coordinator + evaluator get ADR-0090 sets', () => {
    expect(parseWakeActions(undefined, 'worker')).toBeNull()
    expect(parseWakeActions(undefined, '')).toBeNull()
    expect(parseWakeActions(undefined, 'coordinator')).toEqual([
      ...COORDINATOR_DEFAULT_WAKE_ACTIONS,
    ])
    expect(parseWakeActions(undefined, 'evaluator')).toEqual([
      ...EVALUATOR_DEFAULT_WAKE_ACTIONS,
    ])
  })
})

describe('orchestrator default wake set', () => {
  const gate = new WakeGate(parseWakeActions(undefined, 'orchestrator'))

  test('the two ENUMERATED escalation-class actions wake (forbidden-failure-mode guard)', () => {
    // These escape the task.escalat* glob; ADR-0089 enumerates them
    // explicitly because a filtered-away escalation must never happen.
    expect(gate.wakes('task.evaluator_timeout')).toBe(true)
    expect(gate.wakes('task.rework_exhausted')).toBe(true)
  })

  test('escalation glob + decision-class events wake', () => {
    expect(gate.wakes('task.escalated_to_operator')).toBe(true)
    expect(gate.wakes('task.escalation_closed')).toBe(true)
    expect(gate.wakes('github.pr_merged')).toBe(true)
    expect(gate.wakes('task.evaluator_verdict')).toBe(true)
    expect(gate.wakes('task.registered')).toBe(true)
    expect(gate.wakes('task.cancelled')).toBe(true)
    expect(gate.wakes('deploy.failed')).toBe(true)
    expect(gate.wakes('staging_smoke.failed')).toBe(true)
    expect(gate.wakes('datamigration.first_success')).toBe(true)
  })

  test('terminal plan outcomes wake; lifecycle echoes stay suppressed (orchestrator ruling)', () => {
    // plan.completed / plan.abandoned are decision-carrying and
    // ENUMERATED BY NAME (not plan.*); registered/activated/submitted
    // are the orchestrator's own submits echoed back — the noise class.
    expect(gate.wakes('plan.completed')).toBe(true)
    expect(gate.wakes('plan.abandoned')).toBe(true)
    expect(gate.wakes('plan.registered')).toBe(false)
    expect(gate.wakes('plan.activated')).toBe(false)
    expect(gate.wakes('plan.submitted')).toBe(false)
  })

  test('noise classes are suppressed', () => {
    expect(gate.wakes('github.check_run_completed')).toBe(false)
    expect(gate.wakes('github.pr_synchronize')).toBe(false)
    expect(gate.wakes('step.started')).toBe(false)
    expect(gate.wakes('deploy.succeeded')).toBe(false)
  })

  test('unfiltered gate wakes everything', () => {
    const open = new WakeGate(null)
    expect(open.filtered).toBe(false)
    expect(open.wakes('github.check_run_completed')).toBe(true)
    expect(open.shouldDeliver('github.pr_synchronize')).toBe(true)
  })
})

// ── suppression digest ──────────────────────────────────────────────────────

describe('suppression digest', () => {
  test('accumulates per-action counts and distinct tasks, then resets on delivery', () => {
    const gate = new WakeGate(['github.pr_merged'])
    for (let i = 0; i < 3; i++) {
      expect(gate.shouldDeliver('github.check_run_completed', 'task-a')).toBe(false)
    }
    expect(gate.shouldDeliver('github.pr_synchronize', 'task-b')).toBe(false)
    expect(gate.pendingCount).toBe(4)

    // The next delivered wake carries the digest line (peek is pure)…
    expect(gate.shouldDeliver('github.pr_merged', 'task-a')).toBe(true)
    expect(gate.peekDigest()).toBe(
      'suppressed since last wake: 3 github.check_run_completed, ' +
        '1 github.pr_synchronize across 2 tasks',
    )

    // …and the window resets only on the post-delivery commit.
    expect(gate.pendingCount).toBe(4)
    gate.markDelivered()
    expect(gate.pendingCount).toBe(0)
    expect(gate.peekDigest()).toBeNull()
  })

  test('digest omits the task suffix when no suppressed event carried a task id', () => {
    const gate = new WakeGate(['github.pr_merged'])
    gate.shouldDeliver('deploy.succeeded', null)
    expect(gate.peekDigest()).toBe('suppressed since last wake: 1 deploy.succeeded')
  })

  test('nothing suppressed → no digest line on delivery', () => {
    const gate = new WakeGate(['github.pr_merged'])
    expect(gate.shouldDeliver('github.pr_merged')).toBe(true)
    expect(gate.peekDigest()).toBeNull()
  })

  test('a failed delivery retains the digest (peek without commit)', () => {
    // PR #310 review hardening: takeDigest()-before-await silently lost
    // the digest when the notification rejected. Peek is pure — only a
    // successful delivery's markDelivered() clears the counters.
    const gate = new WakeGate(['github.pr_merged'])
    gate.shouldDeliver('github.check_run_completed', 'task-a')
    const first = gate.peekDigest()
    expect(first).toBe('suppressed since last wake: 1 github.check_run_completed across 1 task')
    // Notification failed → no markDelivered() → digest still pending.
    expect(gate.peekDigest()).toBe(first)
    expect(gate.pendingCount).toBe(1)
    gate.markDelivered()
    expect(gate.peekDigest()).toBeNull()
  })
})

// ── max-suppression-age (bounded blindness) ─────────────────────────────────

describe('max-suppression-age', () => {
  const AGE = 60 * 60_000

  test('a suppressed-only stream produces ONE digest wake once the age passes', () => {
    let nowMs = 0
    const gate = new WakeGate(['github.pr_merged'], {
      maxSuppressionAgeMs: AGE,
      now: () => nowMs,
    })

    gate.shouldDeliver('github.check_run_completed', 'task-a')
    gate.shouldDeliver('github.check_run_completed', 'task-a')

    // Inside the window: nothing fires (the caller's periodic check
    // returns empty-handed).
    nowMs = AGE - 1
    expect(gate.peekOverdueDigest()).toBeNull()

    // Past the window (age + the caller's next poll tick): one digest.
    nowMs = AGE + 1
    expect(gate.peekOverdueDigest()).toBe(
      'suppressed since last wake: 2 github.check_run_completed across 1 task',
    )

    // A FAILED digest wake (no commit) retries on the next tick…
    expect(gate.peekOverdueDigest()).toBe(
      'suppressed since last wake: 2 github.check_run_completed across 1 task',
    )

    // …and ONE successful wake commits: the next tick is silent.
    gate.markDelivered()
    expect(gate.peekOverdueDigest()).toBeNull()
  })

  test('no overdue digest when nothing is suppressed, however old the window', () => {
    let nowMs = 0
    const gate = new WakeGate(['github.pr_merged'], {
      maxSuppressionAgeMs: AGE,
      now: () => nowMs,
    })
    nowMs = AGE * 10
    expect(gate.peekOverdueDigest()).toBeNull()
  })

  test('a delivered wake re-arms the age window', () => {
    let nowMs = 0
    const gate = new WakeGate(['github.pr_merged'], {
      maxSuppressionAgeMs: AGE,
      now: () => nowMs,
    })
    gate.shouldDeliver('github.check_run_completed')
    nowMs = AGE - 1000
    gate.shouldDeliver('github.pr_merged') // delivered…
    gate.markDelivered() // …successful delivery commits + stamps
    gate.shouldDeliver('github.pr_synchronize')
    nowMs = AGE + 1000 // only ~2s since the delivery — not overdue
    expect(gate.peekOverdueDigest()).toBeNull()
    nowMs = AGE - 1000 + AGE + 1 // a full age after the delivery
    expect(gate.peekOverdueDigest()).toBe(
      'suppressed since last wake: 1 github.pr_synchronize',
    )
  })
})

// ── wake ⊇ relay superset invariant ─────────────────────────────────────────

describe('wakeSetViolations', () => {
  test('violating pair: a wake set missing relay-significant actions is named', () => {
    const violations = wakeSetViolations(['github.pr_merged'], 'quiet')
    expect(violations).toEqual([
      'task.escalated_to_operator',
      'task.evaluator_timeout',
      'task.rework_exhausted',
      'task.cancelled',
    ])
  })

  test('quiet relay set pins the ENUMERATED escalation-class actions (muting guard)', () => {
    // PR #310 blocking finding: a custom wake set that covers the
    // escalated_to_operator action but drops the two enumerated
    // escalation-class wire actions must FAIL the superset check —
    // otherwise the WARN silently passes a config that mutes an
    // escalation class (the one forbidden failure mode).
    const violations = wakeSetViolations(
      ['github.pr_merged', 'task.escalated_to_operator', 'task.cancelled'],
      'quiet',
    )
    expect(violations).toEqual(['task.evaluator_timeout', 'task.rework_exhausted'])
  })

  test('orchestrator defaults satisfy the quiet relay set', () => {
    expect(wakeSetViolations([...ORCHESTRATOR_DEFAULT_WAKE_ACTIONS], 'quiet')).toEqual([])
  })

  test('orchestrator defaults violate the normal relay set (pr_opened never wakes)', () => {
    const violations = wakeSetViolations(
      [...ORCHESTRATOR_DEFAULT_WAKE_ACTIONS],
      'normal',
    )
    expect(violations).toContain('github.pr_opened')
  })

  test('unfiltered set trivially satisfies every level', () => {
    for (const level of ['quiet', 'normal', 'verbose'] as const) {
      expect(wakeSetViolations(null, level)).toEqual([])
    }
  })

  test('relay levels are cumulative (normal ⊇ quiet, verbose ⊇ normal)', () => {
    const { quiet, normal, verbose } = RELAY_SIGNIFICANT_ACTIONS
    for (const a of quiet) expect(normal).toContain(a)
    for (const a of normal) expect(verbose).toContain(a)
  })
})

// ── ADR-0090 role defaults (task fe98030f) ───────────────────────────

describe('coordinator default wake set (ADR-0090)', () => {
  const gate = new WakeGate([...COORDINATOR_DEFAULT_WAKE_ACTIONS])

  test('the two noise classes are EXCLUDED by design', () => {
    expect(gate.wakes('github.check_run_completed')).toBe(false)
    expect(gate.wakes('github.pr_synchronize')).toBe(false)
  })

  test('the ci_result rollup wakes — the replacement for per-check noise', () => {
    expect(gate.wakes('task.ci_result')).toBe(true)
  })

  test('EVERY escalation-class action still wakes (forbidden-failure-mode guard)', () => {
    expect(gate.wakes('task.escalated_to_operator')).toBe(true)
    expect(gate.wakes('task.escalation_acknowledged')).toBe(true)
    expect(gate.wakes('task.evaluator_timeout')).toBe(true)
    expect(gate.wakes('task.rework_exhausted')).toBe(true)
  })

  test('decision + lifecycle classes the §3 handlers key on all wake', () => {
    for (const a of [
      'github.pr_merged', 'github.pr_opened', 'github.pr_review_submitted',
      'task.evaluator_verdict', 'task.registered', 'task.completed',
      'task.retry', 'task.cancelled', 'plan.submitted', 'plan.completed',
      'plan.abandoned', 'deploy.failed', 'staging_smoke.failed',
    ]) {
      expect(gate.wakes(a)).toBe(true)
    }
  })

  test('plan.submitted wakes the coordinator — the #310 pickup signal', () => {
    expect(gate.wakes('plan.submitted')).toBe(true)
  })
})

describe('evaluator default wake set (ADR-0090)', () => {
  const gate = new WakeGate([...EVALUATOR_DEFAULT_WAKE_ACTIONS])

  test('the two noise classes are EXCLUDED by design', () => {
    expect(gate.wakes('github.check_run_completed')).toBe(false)
    expect(gate.wakes('github.pr_synchronize')).toBe(false)
  })

  test('ci_result + review-handoff family wake', () => {
    expect(gate.wakes('task.ci_result')).toBe(true)
    expect(gate.wakes('review.override')).toBe(true)
    expect(gate.wakes('task.evaluator_verdict')).toBe(true)
  })

  test('EVERY escalation-class action still wakes (forbidden-failure-mode guard)', () => {
    expect(gate.wakes('task.escalated_to_operator')).toBe(true)
    expect(gate.wakes('task.evaluator_timeout')).toBe(true)
    expect(gate.wakes('task.rework_exhausted')).toBe(true)
  })

  test('coordinator bookkeeping classes stay out (briefs arrive via relay)', () => {
    expect(gate.wakes('task.registered')).toBe(false)
    expect(gate.wakes('plan.submitted')).toBe(false)
  })
})

describe('wake ⊇ relay for the ADR-0090 role sets', () => {
  test('coordinator satisfies quiet AND normal', () => {
    expect(wakeSetViolations([...COORDINATOR_DEFAULT_WAKE_ACTIONS], 'quiet')).toEqual([])
    expect(wakeSetViolations([...COORDINATOR_DEFAULT_WAKE_ACTIONS], 'normal')).toEqual([])
  })

  test('evaluator satisfies quiet AND normal', () => {
    expect(wakeSetViolations([...EVALUATOR_DEFAULT_WAKE_ACTIONS], 'quiet')).toEqual([])
    expect(wakeSetViolations([...EVALUATOR_DEFAULT_WAKE_ACTIONS], 'normal')).toEqual([])
  })

  test('verbose violations are step.* only — the documented accepted WARN tier', () => {
    expect(wakeSetViolations([...COORDINATOR_DEFAULT_WAKE_ACTIONS], 'verbose')).toEqual([
      'step.started', 'step.completed',
    ])
    expect(wakeSetViolations([...EVALUATOR_DEFAULT_WAKE_ACTIONS], 'verbose')).toEqual([
      'step.started', 'step.completed',
    ])
  })

  test('relay vocabulary dropped the two noise classes with the wake vocabulary', () => {
    for (const level of ['quiet', 'normal', 'verbose'] as const) {
      expect(RELAY_SIGNIFICANT_ACTIONS[level]).not.toContain('github.check_run_completed')
      expect(RELAY_SIGNIFICANT_ACTIONS[level]).not.toContain('github.pr_synchronize')
    }
    expect(RELAY_SIGNIFICANT_ACTIONS.normal).toContain('task.ci_result')
  })
})
