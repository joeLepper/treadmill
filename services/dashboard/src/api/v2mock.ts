/**
 * v2 mock data — the post-ADR-0087 operator surface.
 *
 * Shapes mirror the brief (docs/dashboard/2026-06-11-dashboard-v2-design-brief.md)
 * and the real post-PR-F data sources: team_configs, task_executions,
 * llm_calls, the events table, docs frontmatter. This is the v1 PR-A
 * pattern — visual layer over mock data with correct field shapes — so
 * the screens are honest about what live queries can show before wiring.
 *
 * All "now-relative" ages are computed from import time so the dashboard
 * reads as live on load.
 */

const T0 = Date.now();
const ago = (s: number) => new Date(T0 - s * 1000).toISOString();

// ─── S1 — Team roster ────────────────────────────────────────────────

export type Role = 'coordinator' | 'evaluator' | 'worker';
export type SessionState = 'live' | 'idle' | 'down';

export interface CurrentWork {
  taskId: string;
  title: string;
  trigger: 'initial' | 'coordinator-rework' | 'evaluator-rework' | 'peer-review';
  startedAgeS: number;
  /** Alan's fold: an execution spans until MERGE, not subprocess exit.
   *  A worker legitimately holds an awaiting-merge row + an active one. */
  awaitingMerge?: boolean;
}

export interface Session {
  label: string;
  role: Role;
  state: SessionState;
  lastEventAgeS: number;
  current: CurrentWork[]; // 0..2 rows — see awaitingMerge
  today: { initial: number; rework: number; review: number };
  /** What this session tends to draw — derived from its task history
   *  (the repos/areas it has shipped most). Display-only colour. */
  specialty?: string;
  /** The model this session runs (fleet tiering ADR — workers on the
   *  cheaper tier, coordinator/evaluator on the stronger one). */
  model?: string;
}

/** Fleet model tiering: workers run the cheaper tier, coordinator +
 *  evaluator the stronger one. Used as the default when a session has no
 *  explicit `model` set. */
export function sessionModel(s: Session): string {
  return s.model ?? (s.role === 'worker' ? 'sonnet-4.6' : 'opus-4.8');
}

export interface Team {
  repo: string;
  slug: string;
  coordinator: Session;
  evaluator: Session;
  workers: Session[];
}

export const teams: Team[] = [
  {
    repo: 'RAMJAC/ramjac',
    slug: 'ramjac',
    coordinator: {
      label: 'coordinator-ramjac',
      role: 'coordinator',
      state: 'live',
      lastEventAgeS: 14,
      current: [],
      today: { initial: 0, rework: 0, review: 0 },
    },
    evaluator: {
      label: 'evaluator-ramjac',
      role: 'evaluator',
      state: 'live',
      lastEventAgeS: 191,
      current: [],
      today: { initial: 0, rework: 0, review: 0 },
    },
    workers: [
      {
        label: 'worker-ramjac-1',
        role: 'worker',
        state: 'live',
        lastEventAgeS: 38,
        current: [
          {
            taskId: 'a1c0-pdf-checksum',
            title: 'PDF checksum-parity validator',
            trigger: 'initial',
            startedAgeS: 612,
          },
        ],
        today: { initial: 3, rework: 4, review: 2 },
      },
      {
        label: 'worker-ramjac-2',
        role: 'worker',
        state: 'live',
        lastEventAgeS: 9,
        current: [
          {
            taskId: 'b3f1-outbox-wiring',
            title: 'Outbox local-GCP compose drains',
            trigger: 'coordinator-rework',
            startedAgeS: 244,
          },
          {
            taskId: '7d13-replay-harness',
            title: 'Event-store replay-equivalence harness',
            trigger: 'initial',
            startedAgeS: 1840,
            awaitingMerge: true,
          },
        ],
        today: { initial: 5, rework: 7, review: 3 },
      },
      {
        label: 'worker-ramjac-3',
        role: 'worker',
        state: 'idle',
        lastEventAgeS: 2270,
        current: [],
        today: { initial: 1, rework: 0, review: 4 },
      },
    ],
  },
  {
    repo: 'joeLepper/treadmill',
    slug: 'joelepper-treadmill',
    coordinator: {
      label: 'coordinator-joelepper-treadmill',
      role: 'coordinator',
      state: 'live',
      lastEventAgeS: 52,
      current: [],
      today: { initial: 0, rework: 0, review: 0 },
    },
    evaluator: {
      label: 'evaluator-joelepper-treadmill',
      role: 'evaluator',
      state: 'live',
      lastEventAgeS: 420,
      current: [],
      today: { initial: 0, rework: 0, review: 0 },
    },
    workers: [
      {
        label: 'worker-joelepper-treadmill-1',
        role: 'worker',
        state: 'live',
        lastEventAgeS: 16,
        specialty: 'event plumbing · wake/idle filters',
        current: [
          {
            taskId: 'c2a4-wake-filter',
            title: 'Wake-class filter + digest',
            trigger: 'peer-review',
            startedAgeS: 96,
          },
        ],
        today: { initial: 2, rework: 1, review: 5 },
      },
      {
        label: 'worker-joelepper-treadmill-2',
        role: 'worker',
        state: 'live',
        lastEventAgeS: 73,
        specialty: 'API + asyncpg · token metering',
        current: [
          {
            taskId: 'd9e2-harvester',
            title: 'llm_calls harvester + report',
            trigger: 'initial',
            startedAgeS: 410,
          },
        ],
        today: { initial: 2, rework: 2, review: 1 },
      },
      {
        label: 'worker-joelepper-treadmill-3',
        role: 'worker',
        state: 'idle',
        lastEventAgeS: 3100,
        specialty: 'CLI surfaces · cost reporting',
        current: [],
        today: { initial: 1, rework: 0, review: 2 },
      },
    ],
  },
];

// ── Worker detail — specialty + step-wise contributions on current task ──

export interface WorkerProfile {
  /** One-line current focus (what task, what they're doing in it). */
  focus: string;
  /** Lifetime-ish stats, derived from this session's task history. */
  lifetime: { merged: number; reworkRate: number; avgCostPerTask: number };
  /** The steps this session has contributed ON THE CURRENT TASK — the
   *  task_executions where actor == this session, oldest first. Cycles
   *  with a stepId carry a captured transcript (drill in to the
   *  conversation), exactly as the plan-detail loop view. */
  contributions: TaskCycle[];
}

/** Keyed by session label. Only sessions with a profile are drill-in-able;
 *  the rest show identity + today's tallies only. worker-2's harvester
 *  steps point at the REAL transcript fixtures. */
export const workerProfiles: Record<string, WorkerProfile> = {
  'worker-joelepper-treadmill-2': {
    focus: 'building the llm_calls harvester + /report endpoint (ADR-0089)',
    lifetime: { merged: 34, reworkRate: 0.41, avgCostPerTask: 1.92 },
    contributions: [
      { kind: 'dispatch', outcome: 'pass', label: 'initial implementation', actor: 'worker-joelepper-treadmill-2', durationS: 7085, costUsd: 1.08, stepId: '0a6df818-7ad1-4847-a736-6c6d38cf43a9', detail: 'studied ADR-0089 + the plan, built the harvester walk + asyncpg bulk insert + /report rollup' },
      { kind: 'ci', outcome: 'fail', label: 'CI · run 1', actor: 'github', durationS: 300, detail: 'asyncpg insert exceeded the 32767 bind-arg cap on large harvests' },
      { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-joelepper-treadmill-2', durationS: 1640, costUsd: 0.71, stepId: '9b3bd5f4-3a9a-4df5-a8eb-8043ed26d353', detail: 'chunked the INSERT under the bind-arg cap (#315)' },
      { kind: 'ci', outcome: 'pass', label: 'CI · run 2', actor: 'github', durationS: 280 },
      { kind: 'review', outcome: 'lgtm', label: 'peer review', actor: 'worker-joelepper-treadmill-1', durationS: 540, costUsd: 0.3, detail: 'lgtm — chunk size leaves headroom, report query indexed' },
    ],
  },
  'worker-joelepper-treadmill-1': {
    focus: 'wake-class filter + suppressed-event digest (peer-review round)',
    lifetime: { merged: 28, reworkRate: 0.33, avgCostPerTask: 1.55 },
    contributions: [
      { kind: 'dispatch', outcome: 'pass', label: 'initial implementation', actor: 'worker-joelepper-treadmill-1', durationS: 5200, costUsd: 0.94, detail: 'wake-class predicate + digest of suppressed events since last wake' },
      { kind: 'review', outcome: 'lgtm', label: 'peer review · round 1', actor: 'worker-joelepper-treadmill-3', durationS: 600, costUsd: 0.34, detail: 'lgtm pending one nit on the cooldown bound' },
    ],
  },
  // Coordinator/evaluator don't execute task steps — their "contributions"
  // are the lifecycle actions they own (dispatch/merge/bookkeeping for the
  // coordinator; verdicts for the evaluator). Shown as recent activity.
  'coordinator-joelepper-treadmill': {
    focus: 'routing the treadmill team — dispatch, PR bookkeeping, merges',
    lifetime: { merged: 41, reworkRate: 0, avgCostPerTask: 0.12 },
    contributions: [
      { kind: 'dispatch', outcome: 'pass', label: 'dispatched harvester', actor: 'coordinator-joelepper-treadmill', durationS: 12, detail: 'brief → worker-2 (task_execution.initial), registered step start' },
      { kind: 'merge', outcome: 'merged', label: 'merged #318', actor: 'coordinator-joelepper-treadmill', durationS: 8, detail: 'wake-filter — 2/2 lgtm + approve, auto-merge armed' },
      { kind: 'merge', outcome: 'backfill', label: 'merged #1334 · Path-B', actor: 'coordinator-joelepper-treadmill', durationS: 30, detail: 'webhook missed; gh pr view confirmed merged → manual pr_merged event' },
      { kind: 'dispatch', outcome: 'pass', label: 'routed review #1340', actor: 'coordinator-joelepper-treadmill', durationS: 10, detail: 'worker-1 + worker-3 ← peer review' },
    ],
  },
  'evaluator-joelepper-treadmill': {
    focus: 'rendering merge verdicts for the treadmill team',
    lifetime: { merged: 0, reworkRate: 0, avgCostPerTask: 0.38 },
    contributions: [
      { kind: 'eval', outcome: 'approve', label: 'verdict · wake-filter', actor: 'evaluator-joelepper-treadmill', durationS: 540, costUsd: 0.41, detail: 'approve — digest bound correct, cooldown nit resolved' },
      { kind: 'eval', outcome: 'approve', label: 'verdict · nightly matrix', actor: 'evaluator-joelepper-treadmill', durationS: 840, costUsd: 0.36, detail: 'approve — first-real-runtime evidence linked' },
      { kind: 'eval', outcome: 'changes', label: 'verdict · harvester', actor: 'evaluator-joelepper-treadmill', durationS: 420, costUsd: 0.29, detail: 'changes — wanted the bind-arg cap covered by a test before merge' },
    ],
  },
};

// ─── S5 — Loop activity feed ─────────────────────────────────────────

export type FeedKind =
  | 'dispatch'
  | 'ci'
  | 'review'
  | 'verdict'
  | 'merge'
  | 'escalation'
  | 'deploy'
  | 'digest';

export interface FeedEvent {
  id: string;
  ageS: number;
  repo: string;
  team: string;
  kind: FeedKind;
  action: string;
  summary: string;
  /** deploy/smoke rows carry a GitHub run link (Carla's run_url fold). */
  runUrl?: string;
  /** When the event belongs to a plan we hold, clicking the row lands on
   *  that loop (deep-linked to the task). Events without a mapped plan
   *  fall back to the loop board. */
  planId?: string;
  taskId?: string;
}

export const feed: FeedEvent[] = [
  { id: 'e1', ageS: 9, repo: 'joeLepper/treadmill', team: 'joelepper-treadmill', kind: 'dispatch', action: 'task_execution.initial', summary: 'worker-2 ← llm_calls harvester + report', taskId: 'd9e2-harvester' },
  { id: 'e2', ageS: 14, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'ci', action: 'task.ci_result', summary: 'b3f1 outbox-wiring · check_run failure → coordinator-rework', taskId: 'b3f1-outbox-wiring' },
  { id: 'e3', ageS: 38, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'verdict', action: 'task.evaluator_verdict', summary: '7d13 replay-harness · approve', taskId: '7d13-replay-harness' },
  { id: 'e4', ageS: 52, repo: 'joeLepper/treadmill', team: 'joelepper-treadmill', kind: 'review', action: 'task.peer_review_verdict', summary: 'c2a4 wake-filter · 2/2 lgtm → evaluator', taskId: 'c2a4-wake-filter' },
  { id: 'e5', ageS: 96, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'merge', action: 'github.pr_merged', summary: '#1336 dead-code audit · coordinator-merged', planId: 'plan-hygiene', taskId: 'dead-code-audit' },
  { id: 'e6', ageS: 130, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'deploy', action: 'staging_smoke.passed', summary: 'pat-652419 chain smoke · PARITY OK · 8/8', runUrl: 'https://github.com/RAMJAC/ramjac/actions/runs/27292608254' },
  { id: 'e7', ageS: 188, repo: 'joeLepper/treadmill', team: 'joelepper-treadmill', kind: 'digest', action: 'wake.suppressed', summary: 'suppressed since last wake: 47 check_run_completed, 3 pr_synchronize' },
  { id: 'e8', ageS: 240, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'dispatch', action: 'task_execution.peer-review', summary: 'worker-1 + worker-3 ← review #1340' },
  { id: 'e9', ageS: 410, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'escalation', action: 'task.evaluator_timeout', summary: 'a1c0 pdf-checksum · no verdict 30m → orchestrator', taskId: 'a1c0-pdf-checksum' },
  { id: 'e10', ageS: 612, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'deploy', action: 'deploy.succeeded', summary: 'staging · 8 services · digest sha256:98b6…', runUrl: 'https://github.com/RAMJAC/ramjac/actions/runs/27291100021' },
  { id: 'e11', ageS: 905, repo: 'joeLepper/treadmill', team: 'joelepper-treadmill', kind: 'merge', action: 'github.pr_merged', summary: '#1334 nightly matrix · coordinator-merged', planId: 'plan-hygiene', taskId: 'nightly-matrix' },
  { id: 'e12', ageS: 1320, repo: 'RAMJAC/ramjac', team: 'ramjac', kind: 'verdict', action: 'task.evaluator_verdict', summary: '1310 outbox substrate · approve' },
];

// ─── S2 — Loop pipeline (task board) ─────────────────────────────────

export type Stage = 'dispatched' | 'ci' | 'review' | 'evaluator' | 'merged';
export type Bucket = 'blocked' | 'inflight' | 'hopper';

export interface PipelineTask {
  id: string;
  title: string;
  repo: string;
  worker: string;
  stage: Stage;
  bucket: Bucket;
  reworkCount: number;
  reviewCount: number;
  ageS: number;
  awaitingMerge?: boolean;
  backfilled?: boolean; // Alan's fold — Path-B manual backfill badge
  prNumber?: number;
}

export const pipeline: PipelineTask[] = [
  { id: 'b3f1-outbox-wiring', title: 'Outbox local-GCP compose drains', repo: 'RAMJAC/ramjac', worker: 'worker-2', stage: 'ci', bucket: 'inflight', reworkCount: 2, reviewCount: 0, ageS: 244, prNumber: 1359 },
  { id: '7d13-replay-harness', title: 'Event-store replay-equivalence harness', repo: 'RAMJAC/ramjac', worker: 'worker-2', stage: 'merged', bucket: 'inflight', reworkCount: 0, reviewCount: 2, ageS: 1840, awaitingMerge: true, prNumber: 1360 },
  { id: 'a1c0-pdf-checksum', title: 'PDF checksum-parity validator', repo: 'RAMJAC/ramjac', worker: 'worker-1', stage: 'evaluator', bucket: 'blocked', reworkCount: 0, reviewCount: 1, ageS: 612, prNumber: 1361 },
  { id: 'rds-diff', title: 'RDS schema-aware snapshot-diff validator', repo: 'RAMJAC/ramjac', worker: '—', stage: 'dispatched', bucket: 'hopper', reworkCount: 0, reviewCount: 0, ageS: 30 },
  { id: 'c2a4-wake-filter', title: 'Wake-class filter + digest', repo: 'joeLepper/treadmill', worker: 'worker-1', stage: 'review', bucket: 'inflight', reworkCount: 1, reviewCount: 2, ageS: 96, prNumber: 318 },
  { id: 'd9e2-harvester', title: 'llm_calls harvester + report', repo: 'joeLepper/treadmill', worker: 'worker-2', stage: 'ci', bucket: 'inflight', reworkCount: 0, reviewCount: 0, ageS: 410, prNumber: 319 },
  { id: 'cadence-tmpl', title: 'Cache-aware cadence convention', repo: 'joeLepper/treadmill', worker: '—', stage: 'dispatched', bucket: 'hopper', reworkCount: 0, reviewCount: 0, ageS: 18 },
  { id: '1334-nightly', title: 'Nightly full-test matrix', repo: 'RAMJAC/ramjac', worker: 'worker-1', stage: 'merged', bucket: 'inflight', reworkCount: 2, reviewCount: 1, ageS: 5400, backfilled: true, prNumber: 1334 },
];

export const STAGES: Stage[] = ['dispatched', 'ci', 'review', 'evaluator', 'merged'];

// ─── S3 — Cost per outcome (the headline) ────────────────────────────

export interface CostCycle {
  trigger: 'initial' | 'coordinator-rework' | 'evaluator-rework' | 'peer-review';
  usd: number;
  inputTok: number;
  outputTok: number;
  cacheReadTok: number;
}

export interface TaskCost {
  id: string;
  title: string;
  repo: string;
  outcome: 'merged' | 'done';
  prNumber?: number;
  cycles: CostCycle[];
}

export const taskCosts: TaskCost[] = [
  {
    id: '1334-nightly', title: 'Nightly full-test matrix', repo: 'RAMJAC/ramjac', outcome: 'merged', prNumber: 1334,
    cycles: [
      { trigger: 'initial', usd: 1.42, inputTok: 38_000, outputTok: 210_000, cacheReadTok: 4_100_000 },
      { trigger: 'coordinator-rework', usd: 0.81, inputTok: 12_000, outputTok: 96_000, cacheReadTok: 2_600_000 },
      { trigger: 'coordinator-rework', usd: 0.74, inputTok: 11_000, outputTok: 88_000, cacheReadTok: 2_400_000 },
      { trigger: 'peer-review', usd: 0.33, inputTok: 8_000, outputTok: 36_000, cacheReadTok: 1_100_000 },
    ],
  },
  {
    id: '1336-deadcode', title: 'Dead-code audit', repo: 'RAMJAC/ramjac', outcome: 'merged', prNumber: 1336,
    cycles: [
      { trigger: 'initial', usd: 1.91, inputTok: 44_000, outputTok: 280_000, cacheReadTok: 5_200_000 },
      { trigger: 'coordinator-rework', usd: 0.62, inputTok: 9_000, outputTok: 71_000, cacheReadTok: 1_900_000 },
      { trigger: 'peer-review', usd: 0.41, inputTok: 8_500, outputTok: 44_000, cacheReadTok: 1_300_000 },
      { trigger: 'peer-review', usd: 0.38, inputTok: 8_200, outputTok: 41_000, cacheReadTok: 1_250_000 },
    ],
  },
  {
    id: '1271-cloudrun', title: 'Cloud Run outbox service', repo: 'RAMJAC/ramjac', outcome: 'merged', prNumber: 1271,
    cycles: [
      { trigger: 'initial', usd: 2.18, inputTok: 51_000, outputTok: 320_000, cacheReadTok: 6_100_000 },
      { trigger: 'peer-review', usd: 0.44, inputTok: 8_800, outputTok: 47_000, cacheReadTok: 1_400_000 },
      { trigger: 'peer-review', usd: 0.40, inputTok: 8_400, outputTok: 43_000, cacheReadTok: 1_350_000 },
    ],
  },
  {
    id: '1310-substrate', title: 'Outbox substrate', repo: 'RAMJAC/ramjac', outcome: 'merged', prNumber: 1310,
    cycles: [
      { trigger: 'initial', usd: 1.05, inputTok: 28_000, outputTok: 150_000, cacheReadTok: 3_000_000 },
      { trigger: 'evaluator-rework', usd: 0.58, inputTok: 9_500, outputTok: 64_000, cacheReadTok: 1_800_000 },
    ],
  },
];

export interface PlanCost {
  id: string;
  title: string;
  repo: string;
  deliveredTasks: number;
  totalTasks: number;
  usd: number;
}

export const planCosts: PlanCost[] = [
  { id: 'p-outbox', title: 'Outbox + per-service migrations', repo: 'RAMJAC/ramjac', deliveredTasks: 9, totalTasks: 14, usd: 18.42 },
  { id: 'p-adr0087', title: 'ADR-0087 team execution model', repo: 'joeLepper/treadmill', deliveredTasks: 13, totalTasks: 13, usd: 22.07 },
  { id: 'p-hygiene', title: 'Repo hygiene (audit + nightly)', repo: 'RAMJAC/ramjac', deliveredTasks: 2, totalTasks: 2, usd: 6.88 },
  { id: 'p-staging', title: 'GCP staging stand-up', repo: 'RAMJAC/ramjac', deliveredTasks: 3, totalTasks: 5, usd: 9.30 },
];

/** Cache-read unit price is ~10% of input price — the brief's load-bearing
 *  flag. These are placeholder rates; the real per-model table is config. */
export const PRICING_NOTE =
  'cache reads priced at 0.1× input rate · ~85% of token volume · per-model table is config, not the flat v1 ratio';

// ── S3 hero — trend, decomposition, model + cache economics ──────────

export interface DayCost {
  /** days-ago index, 0 = today */
  d: number;
  usd: number;
  outcomes: number; // merged PRs that day
}

/** 14-day window; cost/outcome drifting DOWN as the loop tightens (the
 *  signal Joe wants to tune against). */
export const dailyCost: DayCost[] = [
  { d: 13, usd: 14.2, outcomes: 3 }, { d: 12, usd: 11.8, outcomes: 3 },
  { d: 11, usd: 17.5, outcomes: 5 }, { d: 10, usd: 9.4, outcomes: 3 },
  { d: 9, usd: 12.1, outcomes: 4 }, { d: 8, usd: 8.7, outcomes: 3 },
  { d: 7, usd: 15.9, outcomes: 6 }, { d: 6, usd: 10.2, outcomes: 4 },
  { d: 5, usd: 7.8, outcomes: 4 }, { d: 4, usd: 13.4, outcomes: 7 },
  { d: 3, usd: 9.1, outcomes: 5 }, { d: 2, usd: 11.6, outcomes: 7 },
  { d: 1, usd: 8.3, outcomes: 6 }, { d: 0, usd: 6.9, outcomes: 5 },
];

export interface ModelSpend {
  model: string;
  usd: number;
  calls: number;
}

export const modelSpend: ModelSpend[] = [
  { model: 'opus-4.8', usd: 71.4, calls: 4_820 },
  { model: 'sonnet-4.6', usd: 22.1, calls: 6_140 },
  { model: 'haiku-4.5', usd: 3.2, calls: 2_452 },
];

export interface RepoSpend {
  repo: string;
  usd: number;
  outcomes: number;
}

export const repoSpend: RepoSpend[] = [
  { repo: 'RAMJAC/ramjac', usd: 64.3, outcomes: 41 },
  { repo: 'joeLepper/treadmill', usd: 32.4, outcomes: 24 },
];

/** Aggregate token economics for the window — cache dominates volume but
 *  not cost, the inversion the pricing table must encode. */
export const tokenEconomics = {
  inputTok: 2_140_000,
  outputTok: 9_980_000,
  cacheReadTok: 86_400_000,
  cacheCreationTok: 3_200_000,
  get cacheShareOfVolume() {
    return this.cacheReadTok / (this.inputTok + this.outputTok + this.cacheReadTok + this.cacheCreationTok);
  },
  get hitRatio() {
    return this.cacheReadTok / (this.cacheReadTok + this.inputTok);
  },
};

/** Cost-per-outcome for current vs prior 7-day window (the trend signal). */
export const costPerOutcomeTrend = {
  current: 1.49, // $/merged PR, last 7d
  prior: 2.07, // $/merged PR, prior 7d
  get deltaPct() {
    return (this.current - this.prior) / this.prior;
  },
};

// ─── S4 — Drafts ledger (pipeline of intent) ─────────────────────────

export type DocKind = 'ADR' | 'Plan';
export type IntentStage = 'draft' | 'review' | 'pr-open' | 'merged' | 'submitted' | 'executing' | 'done';

export interface LedgerDoc {
  id: string;
  kind: DocKind;
  title: string;
  repo: string;
  owner: string;
  reviewer: string;
  stage: IntentStage;
  updatedAgeS: number;
  prNumber?: number;
}

export const INTENT_STAGES: IntentStage[] = ['draft', 'review', 'pr-open', 'merged', 'submitted', 'executing', 'done'];

export const ledger: LedgerDoc[] = [
  { id: 'adr-0095', kind: 'ADR', title: 'Queue-depth consumer scaling (push)', repo: 'RAMJAC/ramjac', owner: 'bert', reviewer: 'donna, alan', stage: 'review', updatedAgeS: 600 },
  { id: 'brief-dashv2', kind: 'Plan', title: 'Dashboard v2 design brief', repo: 'joeLepper/treadmill', owner: 'bert', reviewer: 'alan, carla, donna', stage: 'review', updatedAgeS: 1800 },
  { id: 'plan-g3', kind: 'Plan', title: 'Prod-promotion pipeline (GitHub Environments)', repo: 'RAMJAC/ramjac', owner: 'carla', reviewer: 'bert', stage: 'pr-open', updatedAgeS: 2400, prNumber: 1357 },
  { id: 'plan-staging', kind: 'Plan', title: 'GCP staging stand-up', repo: 'RAMJAC/ramjac', owner: 'carla', reviewer: 'bert, donna', stage: 'submitted', updatedAgeS: 5400, prNumber: 1301 },
  { id: 'adr-0093', kind: 'ADR', title: 'py→ts feasibility triage', repo: 'RAMJAC/ramjac', owner: 'bert', reviewer: 'donna', stage: 'merged', updatedAgeS: 7200, prNumber: 1335 },
  { id: 'adr-0092', kind: 'ADR', title: 'AWS→GCP data migration', repo: 'RAMJAC/ramjac', owner: 'donna', reviewer: 'bert, carla', stage: 'merged', updatedAgeS: 9000, prNumber: 1321 },
  { id: 'plan-decomm', kind: 'Plan', title: 'AWS decommission (code)', repo: 'RAMJAC/ramjac', owner: 'carla', reviewer: 'bert, donna', stage: 'merged', updatedAgeS: 10800, prNumber: 1268 },
  { id: 'roadmap', kind: 'Plan', title: 'AWS→GCP migration roadmap', repo: 'RAMJAC/ramjac', owner: 'donna', reviewer: 'alan, carla, bert', stage: 'done', updatedAgeS: 14400, prNumber: 1320 },
  { id: 'plan-hygiene', kind: 'Plan', title: 'Repo hygiene (audit + nightly)', repo: 'RAMJAC/ramjac', owner: 'bert', reviewer: 'consensus', stage: 'executing', updatedAgeS: 3600 },
  { id: 'adr-opready', kind: 'ADR', title: 'Operational readiness + alert→runbook', repo: 'RAMJAC/ramjac', owner: 'bert', reviewer: 'alan, donna', stage: 'draft', updatedAgeS: 600 },
  { id: 'plan-cli', kind: 'Plan', title: 'CLI hardening (post-AWS surface)', repo: 'RAMJAC/ramjac', owner: 'bert', reviewer: 'donna', stage: 'draft', updatedAgeS: 900 },
];

// ─── Plan execution — the "loop detail" data (tasks + per-plan metrics) ──

export interface PlanTask {
  id: string;
  title: string;
  stage: Stage;
  bucket: Bucket;
  worker: string;
  reworkCount: number;
  reviewCount: number;
  costUsd: number;
  ageS: number;
  prNumber?: number;
  awaitingMerge?: boolean;
  backfilled?: boolean;
}

export interface PlanRecord {
  id: string; // matches a ledger id + docContent key
  title: string;
  repo: string;
  owner: string;
  reviewer: string;
  stage: IntentStage;
  prNumber?: number;
  tasks: PlanTask[];
}

export const plans: PlanRecord[] = [
  {
    id: 'plan-hygiene', title: 'Repo hygiene (audit + nightly)', repo: 'RAMJAC/ramjac', owner: 'bert', reviewer: 'consensus', stage: 'executing',
    tasks: [
      { id: 'nightly-matrix', title: 'Nightly full-test matrix', stage: 'merged', bucket: 'inflight', worker: 'worker-1', reworkCount: 2, reviewCount: 1, costUsd: 3.30, ageS: 5400, prNumber: 1334, backfilled: true },
      { id: 'dead-code-audit', title: 'Repo-wide dead-code audit', stage: 'merged', bucket: 'inflight', worker: 'worker-2', reworkCount: 1, reviewCount: 2, costUsd: 3.32, ageS: 4900, prNumber: 1336 },
    ],
  },
  {
    id: 'plan-staging', title: 'GCP staging stand-up', repo: 'RAMJAC/ramjac', owner: 'carla', reviewer: 'bert, donna', stage: 'submitted', prNumber: 1301,
    tasks: [
      { id: 'env-contract', title: 'Converge deploy-consumed env contract', stage: 'review', bucket: 'inflight', worker: 'worker-1', reworkCount: 1, reviewCount: 1, costUsd: 2.10, ageS: 3200, prNumber: 1364 },
      { id: 'staging-stack-config', title: 'Fill Pulumi.staging.yaml to dev parity', stage: 'ci', bucket: 'inflight', worker: 'worker-2', reworkCount: 0, reviewCount: 0, costUsd: 1.05, ageS: 1400, prNumber: 1366 },
      { id: 'substrate-up', title: 'Substrate up + promote-to-staging green', stage: 'dispatched', bucket: 'blocked', worker: '—', reworkCount: 0, reviewCount: 0, costUsd: 0, ageS: 600 },
      { id: 'pat-smoke', title: 'pat-652419 staging smoke', stage: 'dispatched', bucket: 'hopper', worker: '—', reworkCount: 0, reviewCount: 0, costUsd: 0, ageS: 120 },
      { id: 'coord-deploy-obs', title: 'Coordinator deploy observability', stage: 'dispatched', bucket: 'hopper', worker: '—', reworkCount: 0, reviewCount: 0, costUsd: 0, ageS: 90 },
    ],
  },
  {
    id: 'plan-g3', title: 'Prod-promotion pipeline (GitHub Environments)', repo: 'RAMJAC/ramjac', owner: 'carla', reviewer: 'bert', stage: 'pr-open', prNumber: 1357,
    tasks: [
      { id: 'detrigger', title: 'De-trigger promote-to-prod auto-fire', stage: 'merged', bucket: 'inflight', worker: 'worker-1', reworkCount: 0, reviewCount: 1, costUsd: 0.92, ageS: 2400, prNumber: 1330 },
      { id: 'dev-preview', title: 'PR → dev preview deploy', stage: 'review', bucket: 'inflight', worker: 'worker-2', reworkCount: 1, reviewCount: 0, costUsd: 1.44, ageS: 1100, prNumber: 1365 },
      { id: 'gh-env-gate', title: 'production environment + required reviewer', stage: 'dispatched', bucket: 'hopper', worker: '—', reworkCount: 0, reviewCount: 0, costUsd: 0, ageS: 300 },
    ],
  },
  {
    id: 'roadmap', title: 'AWS→GCP migration roadmap', repo: 'RAMJAC/ramjac', owner: 'donna', reviewer: 'alan, carla, bert', stage: 'done', prNumber: 1320,
    tasks: [
      { id: 'r-doc', title: 'Roadmap + sequencing + data-validation gate', stage: 'merged', bucket: 'inflight', worker: 'donna', reworkCount: 0, reviewCount: 2, costUsd: 1.10, ageS: 14400, prNumber: 1320 },
    ],
  },
  {
    id: 'plan-decomm', title: 'AWS decommission (code)', repo: 'RAMJAC/ramjac', owner: 'carla', reviewer: 'bert, donna', stage: 'merged', prNumber: 1268,
    tasks: [
      { id: 'd-wave1', title: 'Wave 1 — top-level prisma teardown', stage: 'merged', bucket: 'inflight', worker: 'worker-2', reworkCount: 0, reviewCount: 2, costUsd: 2.40, ageS: 10800, prNumber: 1268 },
    ],
  },
  {
    id: 'brief-dashv2', title: 'Dashboard v2 design brief', repo: 'joeLepper/treadmill', owner: 'bert', reviewer: 'alan, carla, donna', stage: 'review',
    tasks: [],
  },
];

// ── Task journey — the per-task loop story (cycles, not just final state) ──

export type CycleKind = 'dispatch' | 'ci' | 'review' | 'eval' | 'merge';
export type CycleOutcome =
  | 'pass' | 'fail' | 'lgtm' | 'changes' | 'approve' | 'rework' | 'merged' | 'backfill' | 'running';

export interface TaskCycle {
  kind: CycleKind;
  outcome: CycleOutcome;
  label: string; // "CI · run 2", "peer review · round 1"
  actor: string; // worker / evaluator / coordinator
  durationS: number; // time this cycle took / time-in-stage
  costUsd?: number; // attributed from llm_calls (rework/author/review cycles)
  detail?: string; // verdict reasoning / failure reason — real event payload
  /** task_execution_id — when set, this cycle has a captured worker
   *  transcript we can slice (by request_id) and draw the conversation for
   *  JUST this step. Gate cycles (github CI, coordinator merge) have none. */
  stepId?: string;
}

/** Keyed by task id. The merge of a task's task_executions (work cycles)
 *  with its gate events (ci_result / peer_review_verdict / evaluator_verdict)
 *  — the journey including every loop, which the 5-box strip flattens. */
export const taskTimelines: Record<string, TaskCycle[]> = {
  'nightly-matrix': [
    { kind: 'dispatch', outcome: 'merged', label: 'dispatched', actor: 'worker-1', durationS: 7085, costUsd: 1.08, stepId: '0a6df818-7ad1-4847-a736-6c6d38cf43a9', detail: 'worker took the brief, studied ADR-0089 + the plan, built the harvester + meter' },
    { kind: 'ci', outcome: 'fail', label: 'CI · run 1', actor: 'github', durationS: 480, detail: 'nta_extractor unit job — pip backtracking timeout (no -c on the unit runner)' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-1', durationS: 1320, costUsd: 0.81, stepId: '9b3bd5f4-3a9a-4df5-a8eb-8043ed26d353', detail: 'added -c constraints to the unit runner (#1302 only fixed integration)' },
    { kind: 'ci', outcome: 'fail', label: 'CI · run 2', actor: 'github', durationS: 360, detail: 'nightly RED-issue job — gh issue create died on a nonexistent --label' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-1', durationS: 1080, costUsd: 0.74, stepId: '3838673f-1eb8-4976-8d31-8cc8df812a0a', detail: 'dropped --label; added issues:write to the workflow token' },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 3', actor: 'github', durationS: 540, detail: '11/11 green — nta completed in 4m, no longer a zombie' },
    { kind: 'review', outcome: 'lgtm', label: 'peer review', actor: 'worker-3', durationS: 720, costUsd: 0.33, detail: 'lgtm — workflow_dispatch trigger present, timeouts bounded' },
    { kind: 'eval', outcome: 'approve', label: 'evaluator', actor: 'evaluator', durationS: 840, detail: 'approve — first-real-runtime evidence linked (manual dispatch green run)' },
    { kind: 'merge', outcome: 'backfill', label: 'merged · Path-B', actor: 'coordinator', durationS: 60, detail: 'webhook missed; gh pr view confirmed merged → manual pr_merged event' },
  ],
  'dead-code-audit': [
    { kind: 'dispatch', outcome: 'merged', label: 'dispatched', actor: 'worker-2', durationS: 0 },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 1', actor: 'github', durationS: 420 },
    { kind: 'review', outcome: 'changes', label: 'peer review · round 1', actor: 'worker-1', durationS: 600, costUsd: 0.41, detail: 'needs-changes: AWS findings missing the DEFERRED-DELETION marker' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-2', durationS: 840, costUsd: 0.62, detail: 'added DEFERRED-DELETION to every AWS finding; validation gate blocks aws|lambda deletes' },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 2', actor: 'github', durationS: 360 },
    { kind: 'review', outcome: 'lgtm', label: 'peer review · round 2', actor: 'worker-1', durationS: 540, costUsd: 0.38, detail: 'lgtm — markers present, audit doc cross-references aws-decommission' },
    { kind: 'eval', outcome: 'approve', label: 'evaluator', actor: 'evaluator', durationS: 660, detail: 'approve — identify-only honored, nothing deleted in the diff' },
    { kind: 'merge', outcome: 'merged', label: 'merged', actor: 'coordinator', durationS: 45 },
  ],
  // ── loop-board (pipeline) tasks — keyed by PipelineTask.id ──
  'd9e2-harvester': [
    { kind: 'dispatch', outcome: 'pass', label: 'initial implementation', actor: 'worker-2', durationS: 7085, costUsd: 1.08, stepId: '0a6df818-7ad1-4847-a736-6c6d38cf43a9', detail: 'studied ADR-0089 + the plan, built the harvester walk + asyncpg bulk insert + /report rollup' },
    { kind: 'ci', outcome: 'fail', label: 'CI · run 1', actor: 'github', durationS: 300, detail: 'asyncpg insert exceeded the 32767 bind-arg cap on large harvests' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-2', durationS: 1640, costUsd: 0.71, stepId: '9b3bd5f4-3a9a-4df5-a8eb-8043ed26d353', detail: 'chunked the INSERT under the bind-arg cap (#315)' },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 2', actor: 'github', durationS: 280, detail: 'green — harvest of 46k calls completes in 9s' },
  ],
  'b3f1-outbox-wiring': [
    { kind: 'dispatch', outcome: 'pass', label: 'initial implementation', actor: 'worker-2', durationS: 4200, costUsd: 0.96, detail: 'compose drains for the local→GCP outbox bridge' },
    { kind: 'ci', outcome: 'fail', label: 'CI · run 1', actor: 'github', durationS: 360, detail: 'check_run failure — drain race on the local emulator' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-2', durationS: 980, costUsd: 0.58, detail: 'awaited drain ack before teardown' },
    { kind: 'ci', outcome: 'fail', label: 'CI · run 2', actor: 'github', durationS: 320, detail: 'still flaky — emulator startup not gated' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-2', durationS: 760, costUsd: 0.49, detail: 'gated on emulator healthcheck; in CI now' },
  ],
  '1334-nightly': [
    { kind: 'dispatch', outcome: 'merged', label: 'dispatched', actor: 'worker-1', durationS: 7085, costUsd: 1.08, detail: 'nightly full-test matrix workflow' },
    { kind: 'ci', outcome: 'fail', label: 'CI · run 1', actor: 'github', durationS: 480, detail: 'nta_extractor unit job — pip backtracking timeout' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-1', durationS: 1320, costUsd: 0.81, detail: 'added -c constraints to the unit runner' },
    { kind: 'ci', outcome: 'fail', label: 'CI · run 2', actor: 'github', durationS: 360, detail: 'gh issue create died on a nonexistent --label' },
    { kind: 'review', outcome: 'rework', label: 'coordinator-rework', actor: 'worker-1', durationS: 1080, costUsd: 0.74, detail: 'dropped --label; added issues:write' },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 3', actor: 'github', durationS: 540, detail: '11/11 green' },
    { kind: 'review', outcome: 'lgtm', label: 'peer review', actor: 'worker-3', durationS: 720, costUsd: 0.33 },
    { kind: 'eval', outcome: 'approve', label: 'evaluator', actor: 'evaluator', durationS: 840 },
    { kind: 'merge', outcome: 'backfill', label: 'merged · Path-B', actor: 'coordinator', durationS: 60, detail: 'webhook missed; gh pr view confirmed merged' },
  ],
  'c2a4-wake-filter': [
    { kind: 'dispatch', outcome: 'pass', label: 'initial implementation', actor: 'worker-1', durationS: 5200, costUsd: 0.94, detail: 'wake-class predicate + digest of suppressed events since last wake' },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 1', actor: 'github', durationS: 240 },
    { kind: 'review', outcome: 'lgtm', label: 'peer review', actor: 'worker-3', durationS: 600, costUsd: 0.34, detail: '2/2 lgtm — cooldown bound correct → evaluator' },
  ],
  '7d13-replay-harness': [
    { kind: 'dispatch', outcome: 'pass', label: 'initial implementation', actor: 'worker-2', durationS: 6100, costUsd: 1.21, detail: 'event-store replay-equivalence harness' },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 1', actor: 'github', durationS: 360 },
    { kind: 'review', outcome: 'lgtm', label: 'peer review', actor: 'worker-1', durationS: 720, costUsd: 0.42 },
    { kind: 'eval', outcome: 'approve', label: 'evaluator', actor: 'evaluator', durationS: 660, detail: 'approve — replay parity proven against a captured stream' },
  ],
  'a1c0-pdf-checksum': [
    { kind: 'dispatch', outcome: 'pass', label: 'initial implementation', actor: 'worker-1', durationS: 4800, costUsd: 0.88, detail: 'PDF checksum-parity validator' },
    { kind: 'ci', outcome: 'pass', label: 'CI · run 1', actor: 'github', durationS: 300 },
    { kind: 'review', outcome: 'lgtm', label: 'peer review', actor: 'worker-2', durationS: 540, costUsd: 0.31 },
    { kind: 'eval', outcome: 'fail', label: 'evaluator', actor: 'evaluator', durationS: 1800, detail: 'NO VERDICT in 30m — evaluator stalled → escalated to orchestrator' },
  ],
};

export interface PlanMetrics {
  totalCost: number;
  tasksTotal: number;
  tasksDone: number;
  inFlight: number;
  reworkTotal: number;
  reviewTotal: number;
  avgCostPerTask: number;
}

export function planMetrics(p: PlanRecord): PlanMetrics {
  const done = p.tasks.filter((t) => t.stage === 'merged').length;
  const totalCost = p.tasks.reduce((n, t) => n + t.costUsd, 0);
  const withCost = p.tasks.filter((t) => t.costUsd > 0).length;
  return {
    totalCost,
    tasksTotal: p.tasks.length,
    tasksDone: done,
    inFlight: p.tasks.filter((t) => t.bucket === 'inflight' && t.stage !== 'merged').length,
    reworkTotal: p.tasks.reduce((n, t) => n + t.reworkCount, 0),
    reviewTotal: p.tasks.reduce((n, t) => n + t.reviewCount, 0),
    avgCostPerTask: withCost ? totalCost / withCost : 0,
  };
}

// ─── S6 — Escalations ────────────────────────────────────────────────

export type EscReason =
  | 'evaluator_timeout'
  | 'rework_exhausted'
  | 'mergeability_undetermined'
  | 'worker_failure'
  | 'inference_silence'
  | 'dead_puller';

export type EscStatus = 'open' | 'ack' | 'closed';

export interface Escalation {
  taskId: string;
  title: string;
  repo: string;
  reason: EscReason;
  status: EscStatus;
  openedAgeS: number;
  mttrS?: number;
}

export const escalations: Escalation[] = [
  { taskId: 'a1c0-pdf-checksum', title: 'PDF checksum-parity validator', repo: 'RAMJAC/ramjac', reason: 'evaluator_timeout', status: 'open', openedAgeS: 410 },
  { taskId: 'rds-diff', title: 'RDS schema-aware snapshot-diff', repo: 'RAMJAC/ramjac', reason: 'mergeability_undetermined', status: 'ack', openedAgeS: 1200 },
  { taskId: 'svc-mar', title: 'MAR inference-call-count == 0', repo: 'RAMJAC/ramjac', reason: 'inference_silence', status: 'open', openedAgeS: 95 },
  { taskId: 'old-x', title: 'Outbox wiring · 3× rework', repo: 'RAMJAC/ramjac', reason: 'rework_exhausted', status: 'closed', openedAgeS: 9000, mttrS: 2640 },
];

export { T0, ago };
