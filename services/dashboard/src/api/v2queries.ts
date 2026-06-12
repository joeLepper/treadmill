/**
 * v2 query hooks — real treadmill API reads for the post-ADR-0087 surface.
 *
 * The cost hero (S3) is wired here: GET /api/v1/llm_calls/report gives the
 * harvester's real per-label token sums; the pricing module converts them
 * to USD. Everything derived from this is LIVE. Aggregates the report
 * endpoint does not yet serve (per-outcome rollup, per-model split, daily
 * series) remain on the v2mock module, flagged in the UI as estimated so
 * real-vs-derived-vs-mock is never ambiguous.
 *
 * The same `_apiFetch` idiom as src/api/queries.ts; the seam is honest
 * about what is live so the follow-up endpoints have a visible target.
 */

import { useQuery } from '@tanstack/react-query';
import { costOf } from './pricing';

async function _apiFetch<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

// ─── /llm_calls/report ───────────────────────────────────────────────

export interface TokenReportRow {
  session_label: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  cache_hit_ratio: number;
}

export interface TokenReport {
  since: string;
  rows: TokenReportRow[];
  malformed_lines_total: number;
}

export interface LabelSpend extends TokenReportRow {
  usd: number;
}

export interface CostEconomics {
  /** true when the harvester has rows for the window */
  live: boolean;
  windowSpend: number;
  totalCalls: number;
  inputTok: number;
  outputTok: number;
  cacheReadTok: number;
  cacheCreationTok: number;
  cacheShareOfVolume: number;
  hitRatio: number;
  byLabel: LabelSpend[];
  malformedLines: number;
}

/**
 * Real cost economics for the trailing `sinceDays` window. Returns
 * `live: false` (and zeroed aggregates) when the harvester has produced
 * no rows yet — the UI then falls back to the mock hero with a banner.
 */
export function useCostEconomics(sinceDays = 14) {
  const since = new Date(Date.now() - sinceDays * 86_400_000).toISOString();
  return useQuery<CostEconomics>({
    queryKey: ['llm_cost', sinceDays],
    queryFn: async () => {
      const report = await _apiFetch<TokenReport>(`/api/v1/llm_calls/report?since=${encodeURIComponent(since)}`);
      const byLabel: LabelSpend[] = report.rows.map((r) => ({
        ...r,
        // model is per-call; the report rolls up per-label, so we price at
        // the dominant tier (opus) — refine when the report groups by model.
        usd: costOf(
          {
            input_tokens: r.input_tokens,
            output_tokens: r.output_tokens,
            cache_read_tokens: r.cache_read_tokens,
            cache_creation_tokens: r.cache_creation_tokens,
          },
          'opus',
        ),
      })).sort((a, b) => b.usd - a.usd);

      const sum = (f: (r: TokenReportRow) => number) => report.rows.reduce((n, r) => n + f(r), 0);
      const inputTok = sum((r) => r.input_tokens);
      const outputTok = sum((r) => r.output_tokens);
      const cacheReadTok = sum((r) => r.cache_read_tokens);
      const cacheCreationTok = sum((r) => r.cache_creation_tokens);
      const totalVol = inputTok + outputTok + cacheReadTok + cacheCreationTok;

      return {
        live: report.rows.length > 0,
        windowSpend: byLabel.reduce((n, r) => n + r.usd, 0),
        totalCalls: sum((r) => r.calls),
        inputTok,
        outputTok,
        cacheReadTok,
        cacheCreationTok,
        cacheShareOfVolume: totalVol ? cacheReadTok / totalVol : 0,
        hitRatio: cacheReadTok + inputTok ? cacheReadTok / (cacheReadTok + inputTok) : 0,
        byLabel,
        malformedLines: report.malformed_lines_total,
      };
    },
    staleTime: 30_000,
    retry: 1,
  });
}

// ─── /dashboard/overview ─────────────────────────────────────────────
// Purpose-built aggregate: per-account 24h spend, the non-terminal task
// set with bucket counts, and the recent system-event feed. NOTE the
// `fleet` field is the retired pre-ADR-0087 autoscaler model — the team
// roster comes from /team_configs, not here.

export interface OverviewEvent {
  id: string; entity_type: string; action: string;
  task_id: string | null; repo: string; created_at: string; detail: string | null;
}
export interface OverviewTask {
  id: string; title: string; repo: string; repo_mode: string; account: string;
  plan_id: string; derived_status: string; last_activity: string | null;
  started_at: string | null; created_at: string;
}
export interface OverviewAccount { name: string; tokens_24h: number; usd_est_24h: number; }
export interface BucketCounts { blocked: number; inflight: number; hopper: number; total: number; }

export interface Overview {
  live: boolean;
  accounts: OverviewAccount[];
  bucketCounts: BucketCounts;
  tasks: OverviewTask[];
  events: OverviewEvent[];
}

export function useOverview() {
  return useQuery<Overview>({
    queryKey: ['overview'],
    queryFn: async () => {
      const d = await _apiFetch<Omit<Overview, 'live'>>('/api/v1/dashboard/overview');
      return { ...d, live: true };
    },
    staleTime: 15_000,
    retry: 1,
  });
}

/** Classify a raw system event into the feed's closed FeedKind enum. */
function feedKindOf(action: string): import('./v2mock').FeedKind {
  const a = action.toLowerCase();
  if (a.includes('escalat')) return 'escalation';
  if (a.includes('ci_result') || a.includes('check_run')) return 'ci';
  if (a.includes('pr_merged') || a.includes('merged')) return 'merge';
  if (a.includes('verdict')) return 'verdict';
  if (a.includes('review')) return 'review';
  if (a.includes('deploy') || a.includes('smoke')) return 'deploy';
  if (a.includes('suppress') || a.includes('digest') || a.includes('wake')) return 'digest';
  if (a.includes('dispatch') || a.includes('task_execution') || a.includes('step')) return 'dispatch';
  return 'dispatch';
}

/** Map real overview events to the FeedEvent shape the feed rail renders. */
export function feedFromEvents(events: OverviewEvent[]): import('./v2mock').FeedEvent[] {
  const now = Date.now();
  return events.map((e) => ({
    id: e.id,
    ageS: Math.max(0, Math.round((now - Date.parse(e.created_at)) / 1000)),
    repo: e.repo,
    team: e.repo,
    kind: feedKindOf(e.action),
    action: e.action,
    // Prefer detail; else repo + task context; else the action itself
    // (some events carry null repo/task — never render the literal "null").
    summary: e.detail ?? ([e.repo, e.task_id ? e.task_id.slice(0, 8) : null].filter(Boolean).join(' · ') || e.action),
    taskId: e.task_id ?? undefined,
  }));
}

// ─── /escalations (open incidents) ───────────────────────────────────

export interface OpenEscalation {
  task_id: string; repo: string; title: string; opened_at: string; reason: string | null;
}

export function useEscalations() {
  return useQuery<{ live: boolean; open: OpenEscalation[] }>({
    queryKey: ['escalations'],
    queryFn: async () => {
      const open = await _apiFetch<OpenEscalation[]>('/api/v1/escalations');
      return { live: true, open };
    },
    staleTime: 15_000,
    retry: 1,
  });
}

// ─── /tasks + /task_executions → live board rows & journeys ──────────

export interface ApiTask {
  id: string; plan_id: string | null; repo: string; title: string;
  derived_status: string; created_at: string;
}

/** derived_status is "pr_merged" | "done" | "registered" | "<worker>: executing".
 *  Parse it into a worker + bucket + coarse stage for the board. */
export function parseStatus(s: string): { worker: string; bucket: import('./v2mock').Bucket; stage: import('./v2mock').Stage; label: string } {
  if (s.includes('executing')) {
    const worker = s.split(':')[0].trim();
    return { worker, bucket: 'inflight', stage: 'ci', label: 'executing' };
  }
  if (s === 'pr_merged' || s === 'done') return { worker: '—', bucket: 'inflight', stage: 'merged', label: s === 'done' ? 'done' : 'merged' };
  if (s.includes('blocked')) return { worker: '—', bucket: 'blocked', stage: 'dispatched', label: 'blocked' };
  if (s === 'registered') return { worker: '—', bucket: 'hopper', stage: 'dispatched', label: 'registered' };
  return { worker: '—', bucket: 'hopper', stage: 'dispatched', label: s };
}

export interface BoardTask {
  id: string; title: string; repo: string; planId: string | null;
  worker: string; bucket: import('./v2mock').Bucket; stage: import('./v2mock').Stage; statusLabel: string;
}

export function useTasks() {
  return useQuery<{ live: boolean; tasks: BoardTask[] }>({
    queryKey: ['tasks'],
    queryFn: async () => {
      const raw = await _apiFetch<ApiTask[]>('/api/v1/tasks');
      const tasks = raw.map((t) => {
        const p = parseStatus(t.derived_status);
        return { id: t.id, title: t.title, repo: t.repo, planId: t.plan_id, worker: p.worker, bucket: p.bucket, stage: p.stage, statusLabel: p.label };
      });
      return { live: true, tasks };
    },
    staleTime: 15_000,
    retry: 1,
  });
}

/** Server cycle shape from GET /tasks/{id}/journey (executions ⊕ gate events). */
interface ApiJourneyCycle {
  kind: string; outcome: string; label: string; actor: string;
  started_at: string; completed_at: string | null; detail: string | null;
  task_execution_id: string | null;
  input_tokens: number; output_tokens: number; cache_read_tokens: number; model: string | null;
}

function modelTier(model: string | null): 'opus' | 'sonnet' | 'haiku' {
  const m = (model ?? '').toLowerCase();
  if (m.includes('haiku')) return 'haiku';
  if (m.includes('sonnet')) return 'sonnet';
  return 'opus';
}

function cycleFromApi(c: ApiJourneyCycle): import('./v2mock').TaskCycle {
  const end = c.completed_at ? Date.parse(c.completed_at) : Date.now();
  const tokens = c.input_tokens + c.output_tokens + c.cache_read_tokens;
  const cost = tokens > 0
    ? costOf({ input_tokens: c.input_tokens, output_tokens: c.output_tokens, cache_read_tokens: c.cache_read_tokens, cache_creation_tokens: 0 }, modelTier(c.model))
    : undefined;
  return {
    kind: c.kind as import('./v2mock').CycleKind,
    outcome: c.outcome as import('./v2mock').CycleOutcome,
    label: c.label,
    actor: c.actor,
    durationS: Math.max(0, Math.round((end - Date.parse(c.started_at)) / 1000)),
    costUsd: cost,
    detail: c.detail ?? undefined,
    stepId: c.task_execution_id ?? undefined,
  };
}

/** Real loop journey for a task: executions ⊕ gate events ⊕ token cost.
 *  No mock fallback — the caller renders loading / empty / error. */
export function useTaskJourney(taskId: string | undefined) {
  return useQuery<import('./v2mock').TaskCycle[]>({
    queryKey: ['task_journey', taskId],
    enabled: !!taskId,
    queryFn: async () => {
      const d = await _apiFetch<{ task_id: string; cycles: ApiJourneyCycle[] }>(`/api/v1/tasks/${encodeURIComponent(taskId!)}/journey`);
      return d.cycles.map(cycleFromApi);
    },
    staleTime: 15_000,
    retry: 1,
  });
}

// ─── Plans (derived list) + plan detail ──────────────────────────────

export interface ApiPlan {
  id: string; repo: string; doc_path: string; derived_status: string;
  created_by: string; created_at: string;
}

/** Humanize a plan doc_path into a title: drop dir + leading date + ext. */
export function planTitle(docPath: string): string {
  const base = docPath.split('/').pop() ?? docPath;
  return base.replace(/\.md$/, '').replace(/^\d{4}-\d{2}-\d{2}-/, '').replace(/-/g, ' ');
}

export function planStage(s: string | undefined): import('./v2mock').IntentStage {
  switch (s) {
    case 'completed': case 'done': return 'done';
    case 'active': case 'executing': return 'executing';
    case 'submitted': return 'submitted';
    case 'drafting': case 'draft': return 'draft';
    default: return 'executing';
  }
}

export interface PlanRow {
  id: string; title: string; repo: string; stage: import('./v2mock').IntentStage;
  tasksTotal: number; tasksDone: number;
}

const TERMINAL = new Set(['pr_merged', 'done']);

export function usePlans() {
  return useQuery<{ live: boolean; plans: PlanRow[] }>({
    queryKey: ['plans_list'],
    queryFn: async () => {
      const tasks = await _apiFetch<ApiTask[]>('/api/v1/tasks');
      const byPlan = new Map<string, ApiTask[]>();
      for (const t of tasks) {
        if (!t.plan_id) continue;
        if (!byPlan.has(t.plan_id)) byPlan.set(t.plan_id, []);
        byPlan.get(t.plan_id)!.push(t);
      }
      const ids = [...byPlan.keys()];
      const details = await Promise.all(ids.map((id) => _apiFetch<ApiPlan>(`/api/v1/plans/${id}`).catch(() => null)));
      const plans = ids.map((id, i) => {
        const d = details[i];
        const ts = byPlan.get(id)!;
        return {
          id,
          title: planTitle(d?.doc_path ?? id),
          repo: d?.repo ?? ts[0].repo,
          stage: planStage(d?.derived_status),
          tasksTotal: ts.length,
          tasksDone: ts.filter((t) => TERMINAL.has(t.derived_status)).length,
        };
      }).sort((a, b) => b.tasksTotal - a.tasksTotal);
      return { live: true, plans };
    },
    staleTime: 20_000,
    retry: 1,
  });
}

export interface PlanDetailData {
  live: boolean;
  plan: ApiPlan | null;
  tasks: BoardTask[];
}

export function usePlanDetail(planId: string | undefined) {
  return useQuery<PlanDetailData>({
    queryKey: ['plan_detail', planId],
    enabled: !!planId,
    queryFn: async () => {
      const [plan, rawTasks] = await Promise.all([
        _apiFetch<ApiPlan>(`/api/v1/plans/${planId}`).catch(() => null),
        _apiFetch<ApiTask[]>(`/api/v1/plans/${planId}/tasks`).catch(() => [] as ApiTask[]),
      ]);
      const tasks = rawTasks.map((t) => {
        const p = parseStatus(t.derived_status);
        return { id: t.id, title: t.title, repo: t.repo, planId: t.plan_id, worker: p.worker, bucket: p.bucket, stage: p.stage, statusLabel: p.label };
      });
      return { live: true, plan, tasks };
    },
    staleTime: 20_000,
    retry: 1,
  });
}

// ─── /team_configs → live roster ─────────────────────────────────────

export interface ApiTeamConfig {
  id: string; repo: string; coordinator_label: string; evaluator_label: string;
  worker_labels: string[]; created_at: string; updated_at: string;
}

export function useTeamConfigs() {
  return useQuery<{ live: boolean; teams: ApiTeamConfig[] }>({
    queryKey: ['team_configs'],
    queryFn: async () => {
      const teams = await _apiFetch<ApiTeamConfig[]>('/api/v1/team_configs');
      return { live: true, teams };
    },
    staleTime: 30_000,
    retry: 1,
  });
}

// ─── /cost/rollup → daily spend, by-model, outcomes (priced client-side) ──

interface ApiDaily { day: string; model: string; calls: number; input_tokens: number; output_tokens: number; cache_read_tokens: number; }
interface ApiCostRollup { since: string; daily: ApiDaily[]; by_model: ApiDaily[]; outcomes_merged: number; }

function priceTokens(r: { input_tokens: number; output_tokens: number; cache_read_tokens: number }, model: string): number {
  return costOf({ input_tokens: r.input_tokens, output_tokens: r.output_tokens, cache_read_tokens: r.cache_read_tokens, cache_creation_tokens: 0 }, modelTier(model));
}

export interface CostRollup {
  /** Per calendar day: total priced USD + calls (summed across models). */
  daily: { day: string; usd: number; calls: number }[];
  byModel: { model: string; usd: number; calls: number }[];
  outcomesMerged: number;
  windowSpend: number;
}

export function useCostRollup(sinceDays = 14) {
  const since = new Date(Date.now() - sinceDays * 86_400_000).toISOString();
  return useQuery<CostRollup>({
    queryKey: ['cost_rollup', sinceDays],
    queryFn: async () => {
      const d = await _apiFetch<ApiCostRollup>(`/api/v1/cost/rollup?since=${encodeURIComponent(since)}`);
      const byDay = new Map<string, { usd: number; calls: number }>();
      for (const r of d.daily) {
        const day = r.day.slice(0, 10);
        const cur = byDay.get(day) ?? { usd: 0, calls: 0 };
        cur.usd += priceTokens(r, r.model);
        cur.calls += r.calls;
        byDay.set(day, cur);
      }
      const daily = [...byDay.entries()].map(([day, v]) => ({ day, ...v })).sort((a, b) => a.day.localeCompare(b.day));
      const byModel = d.by_model.map((m) => ({ model: m.model.replace(/^claude-/, ''), usd: priceTokens(m, m.model), calls: m.calls })).sort((a, b) => b.usd - a.usd);
      return { daily, byModel, outcomesMerged: d.outcomes_merged, windowSpend: daily.reduce((n, x) => n + x.usd, 0) };
    },
    staleTime: 30_000,
    retry: 1,
  });
}
