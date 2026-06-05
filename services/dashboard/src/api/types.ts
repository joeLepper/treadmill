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

/* ─── Triage findings (ADR-0061) ──────────────────────────────────── */

export type TriageCategory =
  | 'console_error'
  | 'network_failure'
  | 'broken_asset'
  | 'accessibility'
  | 'layout_overflow'
  | 'consistency'
  | 'dead_affordance'
  | 'loading_state'
  | 'other';

export type TriageSeverity = 'high' | 'medium' | 'low';
export type TriageConfidence = 'high' | 'medium' | 'low';
export type TriageDispatchAction =
  | 'dispatched'
  | 'research_only'
  | 'suppressed'
  | 'escalated_to_operator';
export type TriageSuppressionSignal =
  | 'duplicate_open_pr'
  | 'duplicate_recent_finding'
  | 'out_of_scope'
  | 'low_confidence'
  | 'operator_action_required'
  | 'design_intent'
  | 'not_in_design_system';
export type TriageMode = 'periodic' | 'on_demand';
export type TriageOutcomeState =
  | 'pending'
  | 'merged'
  | 'rejected'
  | 'superseded'
  | 'cancelled';

export interface TriageEvidenceSummary {
  console_errors?: number;
  http_4xx?: number;
  http_5xx?: number;
  requestfailed?: number;
  [key: string]: number | undefined;
}

/**
 * Mirrors the Pydantic ``TriageFinding`` schema in
 * ``services/api/treadmill_api/schemas/triage_finding.py``. Field order
 * here intentionally follows the five layers in ADR-0061: provenance,
 * target state, evidence, detector output, dispatcher output, outcome,
 * labels.
 */
export interface TriageFinding {
  // Provenance
  finding_id: string;
  run_id: string;
  created_at: string | null;
  prompt_version: string;
  model: string;
  mode: TriageMode;
  on_demand_request: string | null;

  // Target state
  target_url: string;
  viewport_w: number;
  viewport_h: number;
  git_sha: string;
  api_git_sha: string | null;

  // Evidence
  screenshot_uri: string;
  viewport_png_uri: string | null;
  dom_snapshot_uri: string | null;
  console_log_uri: string;
  network_log_uri: string;
  evidence_summary: TriageEvidenceSummary;

  // Detector output
  category: TriageCategory;
  severity: TriageSeverity;
  confidence: TriageConfidence;
  observation: string;
  evidence_pointer: string;
  proposed_resolution: string;

  // Dispatcher output
  dispatch_action: TriageDispatchAction;
  dispatch_reason: string;
  suppression_signal: TriageSuppressionSignal | null;
  parent_finding_id: string | null;
  dispatched_plan_id: string | null;

  // Outcome (server-projected)
  outcome_state: TriageOutcomeState | null;
  outcome_pr_number: number | null;
  outcome_merged_at: string | null;
  recurrence_count: number;

  // Labels (operator-set; null = "Skip" per ADR-0061 v1 prompt)
  label_is_real_bug: boolean | null;
  label_severity: TriageSeverity | null;
  label_category: TriageCategory | null;
  label_fix_in_dsl: boolean | null;
  label_dispatch_action: TriageDispatchAction | null;
  label_notes: string | null;
  labeled_by: string | null;
  labeled_at: string | null;
  label_guidelines_version: string | null;
}

/** Body posted to ``POST /api/v1/triage/findings/:id/label``. */
export interface TriageLabelInput {
  label_is_real_bug: boolean | null;
  label_severity: TriageSeverity | null;
  label_category: TriageCategory | null;
  label_fix_in_dsl: boolean | null;
  label_notes: string | null;
  labeled_by: string;
}

/* ─── DSPy variant PR review (ADR-0070) ───────────────────────────── */

export type DspyVariantPrLabel = 'merge' | 'revise' | 'drop';
export type DspyVariantPrConfidence = 'high' | 'medium' | 'low';

export interface DspyVariantPrRow {
  id: string;
  created_at: string;
  source_run_id: string;
  source_pr_number: number;
  source_pr_url: string;
  judge_role: string;
  judge_prompt_path: string;
  current_score: number;
  variant_score: number;
  improvement: number;
  patch_diff: string;
  corpus_s3_uri: string;
  llm_label: DspyVariantPrLabel;
  llm_confidence: DspyVariantPrConfidence;
  llm_rationale: string;
  llm_prompt_version: string;
  llm_model: string;
  label_verdict: DspyVariantPrLabel | null;
  label_notes: string | null;
  label_override_reason: string | null;
  labeled_by: string | null;
  labeled_at: string | null;
  label_guidelines_version: string | null;
  outcome_state: string | null;
  outcome_merged_at: string | null;
}

/** Body posted to ``POST /api/v1/review/dspy-variant-pr/:id/label``. */
export interface DspyVariantPrLabelInput {
  label_verdict: DspyVariantPrLabel;
  label_notes?: string | null;
  label_override_reason?: string | null;
  labeled_by: string;
}
