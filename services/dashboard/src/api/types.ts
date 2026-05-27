/**
 * Treadmill dashboard data types.
 *
 * Field shapes mirror the data spec in
 * `docs/plans/2026-05-26-treadmill-dashboard-v1.md` §"Data shapes". The
 * v1 mock layer fakes data that matches this shape; v2 swaps the mock
 * for fetches from `routers/dashboard.py` aggregation endpoints. The
 * page components consume only these types — they don't know or care
 * which side is serving them.
 */

import type { RepoMode } from '../design/chrome';

export interface PipelineStep {
  role: string;
  status: 'done' | 'running' | 'pending' | 'failed' | string;
}

export interface PullRequest {
  pr_number: number;
  branch: string;
  head_sha: string;
  ci_conclusion: 'success' | 'failure' | 'pending' | null;
  review_decision: 'approved' | 'changes_requested' | 'needs-more-info' | null;
  validate_decision: 'pass' | 'fail' | null;
  pr_conflicting: boolean;
  derived_mergeability:
    | 'pending'
    | 'blocked-on-conflict'
    | 'blocked-on-ci'
    | 'blocked-on-review'
    | 'blocked-on-validate'
    | 'mergeable';
}

export interface Task {
  id: string;
  title: string;
  repo: string;
  repo_mode: RepoMode;
  account: string;
  plan_id: string;
  /** `task_status.derived_status` — values per the data spec enum. */
  derived_status: string;
  last_activity: Date | string;
  started_at: Date | string | null;
  created_at: Date | string;
  pipeline: PipelineStep[];
  workflow: string | null;
  pr: PullRequest | null;
  escalated: boolean;
  escalation_reason?: string;
  cost_usd: number;
  tokens: number;
}

export interface Event {
  id: string;
  entity_type:
    | 'plan'
    | 'task'
    | 'step'
    | 'run'
    | 'github'
    | 'schedule'
    | 'validate'
    | 'review';
  action: string;
  task_id: string | null;
  repo: string | null;
  created_at: Date | string;
  /** Free-text human label — never relied on by code. */
  detail?: string;
}

export interface Account {
  name: string;
  tokens_24h: number;
  usd_est_24h: number;
}

export interface Fleet {
  workers_running: number;
  workers_capacity: number;
  autoscaler_last_tick: Date | string;
  autoscaler_alive_since: Date | string;
  scheduler_last_tick: Date | string;
  scheduler_alive_since: Date | string;
}

export interface Escalation {
  task_id: string;
  repo: string;
  title: string;
  escalated_at: Date | string;
  reason?: string;
}

/* ─── Task detail bundle ──────────────────────────────────────────── */

export interface StepOutput {
  summary?: string;
  decision?: string;
  commit_sha?: string;
}

export interface RunStep {
  id: string;
  role_id: string;
  status: 'running' | 'completed' | 'failed' | string;
  started_at: Date | string | null;
  completed_at: Date | string | null;
  duration_s: number | null;
  output: StepOutput | null;
  error?: string;
  tokens: { in: number; out: number };
}

export interface Run {
  id: string;
  workflow_id: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | string;
  started_at: Date | string;
  completed_at: Date | string | null;
  duration_s: number | null;
  steps: RunStep[];
}

export interface Iteration {
  idx: number;
  kind: string;
  label: string;
  trigger: string;
  status: string;
  started_at: Date | string;
  completed_at: Date | string | null;
  duration_s: number | null;
  runs: Run[];
  tokens: number;
}

export interface TaskDetail {
  task: Task;
  runs: Run[];
}

export interface RepoDocs {
  arch: string;
  plans: number;
  last_updated: Date | string;
}

/* ─── Operator buckets — the three questions Overview is organized by */
export type Bucket = 'blocked' | 'inflight' | 'hopper';
