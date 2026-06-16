/**
 * v2 shape contracts — the post-ADR-0087 operator surface.
 *
 * Types, enums, and the few display constants/derivations the v2 screens
 * share. This module ONCE also held mock data arrays (the PR-A "visual over
 * mock" pattern); those are gone — every screen now reads live data
 * (src/api/v2queries.ts for the API reads, src/api/docContent.ts for the
 * ledger derived from local doc fixtures). No fabricated data ships here:
 * a screen with no live data renders loading / empty / error, never a mock.
 *
 * The file keeps the name `v2mock` only to avoid churning ~8 import sites;
 * its contents are now pure contracts + constants.
 */

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

export interface PlanCost {
  id: string;
  title: string;
  repo: string;
  deliveredTasks: number;
  totalTasks: number;
  usd: number;
}

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

export interface ModelSpend {
  model: string;
  usd: number;
  calls: number;
}

export interface RepoSpend {
  repo: string;
  usd: number;
  outcomes: number;
}

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
