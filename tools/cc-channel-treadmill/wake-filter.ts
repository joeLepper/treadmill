/**
 * wake-filter — ADR-0089 wake-class filtering for the treadmill-events
 * channel server.
 *
 * Pure logic only (no MCP, no WS, no I/O) so `bun test` exercises it
 * directly; `treadmill-events.ts` wires it to the live event stream.
 *
 * Three pieces:
 *
 *   * Pattern parsing + glob matching — `TREADMILL_WAKE_ACTIONS` is a
 *     comma-separated list of `entity.action` globs (`*` matches any run
 *     of characters, all other characters literal). When unset, role
 *     defaults apply: `orchestrator` gets `ORCHESTRATOR_DEFAULT_WAKE_ACTIONS`;
 *     every other role (coordinator / evaluator / worker / unset) is
 *     unfiltered — their event consumption is bookkeeping-load-bearing
 *     (ADR-0089 "measure first").
 *   * `WakeGate` — the suppression state machine: counts suppressed
 *     events per `entity.action` (plus distinct task ids), hands the
 *     digest line to the next delivered wake, and implements
 *     max-suppression-age bounded blindness. The clock is injectable so
 *     tests drive time without timers. State is in-memory by design: a
 *     server restart loses suppressed counts, which is acceptable — the
 *     events table is the record (plan 2026-06-11, risks).
 *   * wake ⊇ relay superset check — the ADR-0071 relay level selects
 *     from events that already woke the session, so a wake filter that
 *     drops a relay-significant action can silently mute the operator.
 *     `wakeSetViolations` returns the relay-significant actions a
 *     configured wake set would never deliver; the server WARNs at
 *     startup when the list is non-empty.
 */

export const RELAY_LEVELS = ['quiet', 'normal', 'verbose'] as const
export type RelayLevel = (typeof RELAY_LEVELS)[number]

/**
 * ADR-0089 orchestrator default wake set. Escalation-CLASS actions whose
 * names escape the `task.escalat*` glob are ENUMERATED explicitly — a
 * filtered-away escalation is the one failure mode this design must never
 * have. Audit any new escalation-class action into this list at
 * introduction.
 */
export const ORCHESTRATOR_DEFAULT_WAKE_ACTIONS: readonly string[] = [
  'github.pr_merged',
  'task.*_verdict',
  'task.escalat*',
  // -- enumerated escalation-class actions (escape the escalat* glob) --
  'task.evaluator_timeout',
  'task.rework_exhausted',
  // -------------------------------------------------------------------
  'task.registered',
  'task.cancelled',
  // prod_promotion.* was in the ADR-0089 set but is omitted here: the
  // ADR-0088 prod-promotion gate was unwound 2026-06-11 (superseded by
  // GitHub environment protection) and its event vocabulary removed.
  'deploy.failed',
  'staging_smoke.failed',
  // ADR-0092 first-success validation gates are alerted-class by design.
  'datamigration.*',
]

/**
 * Concrete wire actions the ADR-0071 relay levels treat as significant,
 * used by the wake ⊇ relay superset check. The ADR-0062 escalation
 * reasons (terminal_step_failure, cap_reached, gate_broken, …) are
 * `reason` values ON `task.escalated_to_operator`, not separate actions,
 * so that single action represents the whole escalation class here.
 * Levels are cumulative (normal ⊇ quiet, verbose ⊇ normal).
 */
export const RELAY_SIGNIFICANT_ACTIONS: Record<RelayLevel, readonly string[]> = (() => {
  const quiet = [
    'github.pr_merged',
    'task.escalated_to_operator',
    'task.cancelled',
  ]
  const normal = [
    ...quiet,
    'github.pr_opened',
    'github.pr_review_submitted',
    // a failed check is what enters the ci-fix loop
    'github.check_run_completed',
  ]
  const verbose = [
    ...normal,
    'step.started',
    'step.completed',
    'github.pr_synchronize',
  ]
  return { quiet, normal, verbose }
})()

/** `entity.action` glob → anchored RegExp. `*` matches any run of
 * characters (including `_` and across nothing); everything else is
 * literal — in particular the `.` separator, so `github.pr_merged`
 * does not match `githubXpr_merged`. */
export function globToRegExp(glob: string): RegExp {
  const escaped = glob
    .replace(/[.+?^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*')
  return new RegExp(`^${escaped}$`)
}

/**
 * Resolve the active wake set: the env value when set (comma-separated
 * globs, whitespace-tolerant), else the role default. `null` means
 * unfiltered — every event wakes.
 */
export function parseWakeActions(
  envValue: string | undefined,
  role: string,
): string[] | null {
  const trimmed = (envValue ?? '').trim()
  if (trimmed.length > 0) {
    const patterns = trimmed
      .split(',')
      .map(s => s.trim())
      .filter(s => s.length > 0)
    if (patterns.length > 0) return patterns
  }
  if (role.trim().toLowerCase() === 'orchestrator') {
    return [...ORCHESTRATOR_DEFAULT_WAKE_ACTIONS]
  }
  return null
}

export const DEFAULT_MAX_SUPPRESSION_AGE_MS = 60 * 60_000

/**
 * The per-session suppression state machine (ADR-0089 §1).
 *
 * Lifecycle per event: `shouldDeliver(entityAction, id)` — `true` for
 * allowlisted events (caller delivers the wake and calls `takeDigest()`
 * to claim the pending summary line); `false` for suppressed events
 * (counted into the digest, caller drops the wake).
 *
 * Bounded blindness: `takeOverdueDigest()` returns the digest when
 * suppressed events are pending AND nothing has been delivered for
 * `maxSuppressionAgeMs` — the caller emits ONE self-originated digest
 * wake. Both take-paths reset the counters and re-arm the age window.
 */
export class WakeGate {
  private readonly patterns: RegExp[] | null
  private readonly maxSuppressionAgeMs: number
  private readonly now: () => number
  private counts = new Map<string, number>()
  private suppressedTaskIds = new Set<string>()
  private lastDeliveredMs: number

  constructor(
    patterns: string[] | null,
    opts: { maxSuppressionAgeMs?: number; now?: () => number } = {},
  ) {
    this.patterns = patterns === null ? null : patterns.map(globToRegExp)
    this.maxSuppressionAgeMs =
      opts.maxSuppressionAgeMs ?? DEFAULT_MAX_SUPPRESSION_AGE_MS
    this.now = opts.now ?? Date.now
    // Arm the age window at construction: a fresh server seeing only
    // suppressed events emits its first digest one age after startup.
    this.lastDeliveredMs = this.now()
  }

  /** Whether a filter is active at all (false = unfiltered role). */
  get filtered(): boolean {
    return this.patterns !== null
  }

  /** Pure allowlist check — no state change. */
  wakes(entityAction: string): boolean {
    if (this.patterns === null) return true
    return this.patterns.some(re => re.test(entityAction))
  }

  /** Filter + count: suppressed events accumulate into the digest. */
  shouldDeliver(entityAction: string, taskId?: string | null): boolean {
    if (this.wakes(entityAction)) return true
    this.counts.set(entityAction, (this.counts.get(entityAction) ?? 0) + 1)
    if (taskId) this.suppressedTaskIds.add(taskId)
    return false
  }

  /** Total suppressed events in the current window (for tests/inspection). */
  get pendingCount(): number {
    let n = 0
    for (const c of this.counts.values()) n += c
    return n
  }

  private summary(): string | null {
    if (this.counts.size === 0) return null
    const parts = [...this.counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([action, n]) => `${n} ${action}`)
    const tasks = this.suppressedTaskIds.size
    return (
      `suppressed since last wake: ${parts.join(', ')}` +
      (tasks > 0 ? ` across ${tasks} task${tasks === 1 ? '' : 's'}` : '')
    )
  }

  /** Claim the pending digest line for a wake being delivered NOW.
   * Returns null when nothing was suppressed. Resets the window. */
  takeDigest(): string | null {
    const line = this.summary()
    this.counts.clear()
    this.suppressedTaskIds.clear()
    this.lastDeliveredMs = this.now()
    return line
  }

  /** Bounded blindness: the digest, iff suppressed events are pending and
   * no wake has been delivered for maxSuppressionAgeMs. Resets the window
   * (the self-originated digest wake counts as a delivery). */
  takeOverdueDigest(): string | null {
    if (this.counts.size === 0) return null
    if (this.now() - this.lastDeliveredMs < this.maxSuppressionAgeMs) {
      return null
    }
    return this.takeDigest()
  }
}

/**
 * wake ⊇ relay superset check (ADR-0089 layering invariant): returns the
 * relay-significant actions of `level` that `patterns` would never wake.
 * Empty array = invariant holds. An unfiltered set (null) trivially holds.
 */
export function wakeSetViolations(
  patterns: string[] | null,
  level: RelayLevel,
): string[] {
  if (patterns === null) return []
  const regexes = patterns.map(globToRegExp)
  return RELAY_SIGNIFICANT_ACTIONS[level].filter(
    action => !regexes.some(re => re.test(action)),
  )
}
