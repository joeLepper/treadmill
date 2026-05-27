/**
 * Mock data + helpers, ported verbatim from the Claude Design handoff
 * bundle (`treadmill-mock.jsx`).
 *
 * Phase 1 of the dashboard work ships against this mock so we can land
 * the visual layer without backend dependencies. Phase 2 replaces the
 * functions in `queries.ts` with real `fetch` calls to
 * `services/api/treadmill_api/routers/dashboard.py`; the page components
 * consume `queries.ts` and never reach into this file directly.
 *
 * Field shapes match the data spec verbatim — same enum values, same
 * relationships, same nullability — so the migration in phase 2 is a
 * one-call swap, not a reshape.
 */

import type {
  Account,
  Bucket,
  Escalation,
  Event,
  Fleet,
  Iteration,
  Run,
  Task,
  TaskDetail,
  RepoDocs,
} from './types';

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000);
const secondsAgo = (s: number) => new Date(Date.now() - s * 1000);

export const ACCOUNTS: Account[] = [
  { name: 'personal', tokens_24h: 1_842_103, usd_est_24h: 24.18 },
  { name: 'osmo', tokens_24h: 5_310_788, usd_est_24h: 71.04 },
  { name: 'bunkhouse', tokens_24h: 612_440, usd_est_24h: 9.21 },
  { name: 'scratch', tokens_24h: 96_220, usd_est_24h: 1.42 },
];

export const FLEET: Fleet = {
  workers_running: 7,
  workers_capacity: 12,
  autoscaler_last_tick: secondsAgo(3),
  autoscaler_alive_since: minutesAgo(184),
  scheduler_last_tick: secondsAgo(11),
  scheduler_alive_since: minutesAgo(184),
};

let TASKS: Task[] = [
  {
    id: 'tsk_8f3a2b1c',
    title: 'Migrate auth callbacks to async/await',
    repo: 'osmo/web',
    repo_mode: 'conform',
    account: 'osmo',
    plan_id: 'pln_q3_auth_refresh',
    derived_status: 'blocked-on-ci',
    last_activity: minutesAgo(12),
    started_at: minutesAgo(38),
    created_at: minutesAgo(54),
    pipeline: [
      { role: 'plan', status: 'done' },
      { role: 'code', status: 'done' },
      { role: 'review', status: 'failed' },
    ],
    workflow: 'wf-ci-fix',
    pr: {
      pr_number: 4128,
      branch: 'claude/auth-async-callbacks',
      head_sha: 'd12fab9',
      ci_conclusion: 'failure',
      review_decision: null,
      validate_decision: null,
      pr_conflicting: false,
      derived_mergeability: 'blocked-on-ci',
    },
    escalated: true,
    escalation_reason: 'stuck > 10m on failing CI: e2e-auth',
    cost_usd: 1.42,
    tokens: 96_822,
  },
  {
    id: 'tsk_27d9e4f0',
    title: 'Add dependency graph virtualization for >500 nodes',
    repo: 'treadmill/dashboard',
    repo_mode: 'conform',
    account: 'personal',
    plan_id: 'pln_perf_graph',
    derived_status: 'wf-quick: executing',
    last_activity: secondsAgo(8),
    started_at: minutesAgo(4),
    created_at: minutesAgo(7),
    pipeline: [
      { role: 'plan', status: 'done' },
      { role: 'code', status: 'running' },
      { role: 'review', status: 'pending' },
    ],
    workflow: 'wf-quick',
    pr: null,
    escalated: false,
    cost_usd: 0.28,
    tokens: 18_400,
  },
  {
    id: 'tsk_a01ec773',
    title: 'Rate limit headers for v2 ingest endpoints',
    repo: 'osmo/data-pipeline',
    repo_mode: 'conform',
    account: 'osmo',
    plan_id: 'pln_v2_ingest',
    derived_status: 'awaiting_review',
    last_activity: minutesAgo(3),
    started_at: minutesAgo(22),
    created_at: minutesAgo(31),
    pipeline: [
      { role: 'plan', status: 'done' },
      { role: 'code', status: 'done' },
      { role: 'review', status: 'running' },
    ],
    workflow: 'wf-review',
    pr: {
      pr_number: 982,
      branch: 'claude/ingest-rate-limit-hdrs',
      head_sha: '7c4ea88',
      ci_conclusion: 'success',
      review_decision: null,
      validate_decision: null,
      pr_conflicting: false,
      derived_mergeability: 'blocked-on-review',
    },
    escalated: false,
    cost_usd: 0.71,
    tokens: 41_209,
  },
  {
    id: 'tsk_1b6f5d22',
    title: 'Snapshot restore failing on >2GB volumes',
    repo: 'osmo/data-pipeline',
    repo_mode: 'conform',
    account: 'osmo',
    plan_id: 'pln_snapshot_repro',
    derived_status: 'wf-feedback: executing',
    last_activity: secondsAgo(45),
    started_at: minutesAgo(72),
    created_at: minutesAgo(88),
    pipeline: [
      { role: 'plan', status: 'done' },
      { role: 'code', status: 'done' },
      { role: 'review', status: 'done' },
      { role: 'feedback', status: 'running' },
    ],
    workflow: 'wf-feedback',
    pr: {
      pr_number: 980,
      branch: 'claude/snapshot-restore-fix',
      head_sha: '11ab4c0',
      ci_conclusion: 'success',
      review_decision: 'changes_requested',
      validate_decision: null,
      pr_conflicting: false,
      derived_mergeability: 'blocked-on-review',
    },
    escalated: false,
    cost_usd: 2.14,
    tokens: 124_044,
  },
  {
    id: 'tsk_55c0a911',
    title: 'Document conform vs adapt mode tradeoffs',
    repo: 'treadmill/core',
    repo_mode: 'conform',
    account: 'personal',
    plan_id: 'pln_adr_collation',
    derived_status: 'blocked-on-conflict',
    last_activity: minutesAgo(18),
    started_at: minutesAgo(95),
    created_at: minutesAgo(110),
    pipeline: [
      { role: 'plan', status: 'done' },
      { role: 'code', status: 'done' },
      { role: 'conflict', status: 'failed' },
    ],
    workflow: 'wf-conflict',
    pr: {
      pr_number: 312,
      branch: 'claude/adr-conform-adapt',
      head_sha: 'e7b211a',
      ci_conclusion: 'success',
      review_decision: 'approved',
      validate_decision: null,
      pr_conflicting: true,
      derived_mergeability: 'blocked-on-conflict',
    },
    escalated: true,
    escalation_reason: 'merge conflict unresolved 18m',
    cost_usd: 0.82,
    tokens: 49_001,
  },
  {
    id: 'tsk_3d8e9f0a',
    title: 'Spike: experiment with batch dispatcher fairness',
    repo: 'treadmill/core',
    repo_mode: 'conform',
    account: 'personal',
    plan_id: 'pln_dispatcher_fair',
    derived_status: 'registered',
    last_activity: minutesAgo(2),
    started_at: null,
    created_at: minutesAgo(2),
    pipeline: [
      { role: 'plan', status: 'pending' },
      { role: 'code', status: 'pending' },
      { role: 'review', status: 'pending' },
    ],
    workflow: null,
    pr: null,
    escalated: false,
    cost_usd: 0,
    tokens: 0,
  },
  {
    id: 'tsk_9be24c6d',
    title: 'Upgrade SDK to handle 429 backoff',
    repo: 'third-party/sdk',
    repo_mode: 'adapt',
    account: 'bunkhouse',
    plan_id: 'pln_sdk_429',
    derived_status: 'wf-validate: executing',
    last_activity: secondsAgo(22),
    started_at: minutesAgo(11),
    created_at: minutesAgo(20),
    pipeline: [
      { role: 'plan', status: 'done' },
      { role: 'code', status: 'done' },
      { role: 'review', status: 'done' },
      { role: 'validate', status: 'running' },
    ],
    workflow: 'wf-validate',
    pr: {
      pr_number: 51,
      branch: 'claude/sdk-429-backoff',
      head_sha: 'fa9c001',
      ci_conclusion: 'success',
      review_decision: 'approved',
      validate_decision: null,
      pr_conflicting: false,
      derived_mergeability: 'blocked-on-validate',
    },
    escalated: false,
    cost_usd: 0.55,
    tokens: 32_104,
  },
  {
    id: 'tsk_6e1077af',
    title: 'Migrate dotfiles to chezmoi',
    repo: 'joe/dotfiles',
    repo_mode: 'adapt',
    account: 'scratch',
    plan_id: 'pln_dotfiles_migrate',
    derived_status: 'blocked',
    last_activity: minutesAgo(34),
    started_at: minutesAgo(58),
    created_at: minutesAgo(70),
    pipeline: [
      { role: 'plan', status: 'done' },
      { role: 'code', status: 'running' },
    ],
    workflow: 'wf-quick',
    pr: null,
    escalated: false,
    cost_usd: 0.11,
    tokens: 6_490,
  },
];

let EVENTS: Event[] = [
  {
    id: 'evt_001',
    entity_type: 'task',
    action: 'escalated_to_operator',
    task_id: 'tsk_8f3a2b1c',
    repo: 'osmo/web',
    created_at: minutesAgo(12),
    detail: 'CI failing > 10m',
  },
  {
    id: 'evt_002',
    entity_type: 'step',
    action: 'failed',
    task_id: 'tsk_8f3a2b1c',
    repo: 'osmo/web',
    created_at: minutesAgo(12),
    detail: 'review step · e2e-auth failed',
  },
  {
    id: 'evt_003',
    entity_type: 'github',
    action: 'ci_failed',
    task_id: 'tsk_8f3a2b1c',
    repo: 'osmo/web',
    created_at: minutesAgo(13),
    detail: 'e2e-auth · run #4128',
  },
  {
    id: 'evt_004',
    entity_type: 'step',
    action: 'started',
    task_id: 'tsk_27d9e4f0',
    repo: 'treadmill/dashboard',
    created_at: minutesAgo(4),
    detail: 'code · claude-sonnet-4',
  },
  {
    id: 'evt_005',
    entity_type: 'run',
    action: 'dispatched',
    task_id: 'tsk_27d9e4f0',
    repo: 'treadmill/dashboard',
    created_at: minutesAgo(4),
    detail: 'wf-quick',
  },
  {
    id: 'evt_006',
    entity_type: 'task',
    action: 'escalated_to_operator',
    task_id: 'tsk_55c0a911',
    repo: 'treadmill/core',
    created_at: minutesAgo(18),
    detail: 'merge conflict unresolved 18m',
  },
  {
    id: 'evt_007',
    entity_type: 'github',
    action: 'pr_opened',
    task_id: 'tsk_a01ec773',
    repo: 'osmo/data-pipeline',
    created_at: minutesAgo(22),
    detail: 'PR #982',
  },
  {
    id: 'evt_008',
    entity_type: 'step',
    action: 'started',
    task_id: 'tsk_1b6f5d22',
    repo: 'osmo/data-pipeline',
    created_at: secondsAgo(45),
    detail: 'feedback · responding to review',
  },
  {
    id: 'evt_009',
    entity_type: 'step',
    action: 'started',
    task_id: 'tsk_9be24c6d',
    repo: 'third-party/sdk',
    created_at: secondsAgo(22),
    detail: 'validate · dev-cluster',
  },
  {
    id: 'evt_010',
    entity_type: 'schedule',
    action: 'tick',
    task_id: null,
    repo: null,
    created_at: secondsAgo(11),
    detail: 'scheduler · 4 tasks dispatchable',
  },
  {
    id: 'evt_011',
    entity_type: 'step',
    action: 'completed',
    task_id: 'tsk_a01ec773',
    repo: 'osmo/data-pipeline',
    created_at: minutesAgo(3),
    detail: 'code · 412 LOC · $0.43',
  },
  {
    id: 'evt_012',
    entity_type: 'github',
    action: 'ci_success',
    task_id: 'tsk_a01ec773',
    repo: 'osmo/data-pipeline',
    created_at: minutesAgo(4),
    detail: 'all checks passed',
  },
  {
    id: 'evt_013',
    entity_type: 'task',
    action: 'created',
    task_id: 'tsk_3d8e9f0a',
    repo: 'treadmill/core',
    created_at: minutesAgo(2),
    detail: 'via /author skill',
  },
  {
    id: 'evt_014',
    entity_type: 'plan',
    action: 'dispatched',
    task_id: null,
    repo: 'osmo/data-pipeline',
    created_at: minutesAgo(35),
    detail: 'pln_v2_ingest · 4 tasks',
  },
  {
    id: 'evt_015',
    entity_type: 'schedule',
    action: 'tick',
    task_id: null,
    repo: null,
    created_at: secondsAgo(41),
    detail: 'scheduler · 7 workers active',
  },
];

/* ─── Operator bucketing — Hopper / In-flight / Blocked ────────────── */

export function operatorBucket(task: Task): Bucket {
  const s = task.derived_status ?? '';
  if (task.escalated) return 'blocked';
  if (s.startsWith('blocked')) return 'blocked';
  if (s === 'registered' || s === 'queued') return 'hopper';
  if (s.includes('executing')) return 'inflight';
  if (s === 'awaiting_review') return 'inflight';
  return 'inflight';
}

const TERMINAL = new Set(['done', 'merged', 'validated', 'cancelled']);

export function getNonTerminalTasks(filters: {
  repo?: string;
  bucket?: Bucket;
  account?: string;
  q?: string;
} = {}): Task[] {
  const { repo, bucket, account, q } = filters;
  return TASKS.filter((t) => !TERMINAL.has(t.derived_status))
    .filter((t) => !repo || t.repo === repo)
    .filter((t) => !bucket || operatorBucket(t) === bucket)
    .filter((t) => !account || t.account === account)
    .filter((t) => {
      if (!q) return true;
      const ql = q.toLowerCase();
      return (
        t.title.toLowerCase().includes(ql) ||
        t.id.toLowerCase().includes(ql) ||
        t.repo.toLowerCase().includes(ql)
      );
    })
    .sort(
      (a, b) =>
        new Date(a.last_activity).getTime() - new Date(b.last_activity).getTime(),
    );
}

export function bucketCounts(): { blocked: number; inflight: number; hopper: number; total: number } {
  const all = TASKS.filter((t) => !TERMINAL.has(t.derived_status));
  return {
    blocked: all.filter((t) => operatorBucket(t) === 'blocked').length,
    inflight: all.filter((t) => operatorBucket(t) === 'inflight').length,
    hopper: all.filter((t) => operatorBucket(t) === 'hopper').length,
    total: all.length,
  };
}

export function getEscalations(): Escalation[] {
  return TASKS.filter((t) => t.escalated).map((t) => ({
    task_id: t.id,
    repo: t.repo,
    title: t.title,
    escalated_at: t.last_activity,
    reason: t.escalation_reason,
  }));
}

export function getEvents(): Event[] {
  return EVENTS;
}

export function getTasks(): Task[] {
  return TASKS;
}

/* ─── Iteration semantics — loop counting on Task Detail ──────────── */

const ITERATION_KINDS = new Set(['wf-quick', 'wf-feedback', 'wf-ci-fix', 'wf-conflict']);
const ITERATION_LABEL: Record<string, string> = {
  'wf-quick': 'initial',
  'wf-feedback': 'review·feedback',
  'wf-ci-fix': 'ci·fix',
  'wf-conflict': 'conflict·resolve',
};
const ITERATION_TRIGGER: Record<string, string> = {
  'wf-quick': 'task dispatched',
  'wf-feedback': 'review requested changes',
  'wf-ci-fix': 'ci checks failed',
  'wf-conflict': 'merge conflict appeared',
};

export function deriveIterations(runs: Run[]): Iteration[] {
  const iters: Iteration[] = [];
  let idx = 0;
  for (const r of runs) {
    if (ITERATION_KINDS.has(r.workflow_id)) {
      idx += 1;
      iters.push({
        idx,
        kind: r.workflow_id,
        label: ITERATION_LABEL[r.workflow_id] ?? r.workflow_id,
        trigger: ITERATION_TRIGGER[r.workflow_id] ?? '—',
        status: r.status,
        started_at: r.started_at,
        completed_at: r.completed_at,
        duration_s: r.duration_s,
        runs: [r],
        tokens: r.steps.reduce(
          (a, s) => a + (s.tokens?.in ?? 0) + (s.tokens?.out ?? 0),
          0,
        ),
      });
    } else if (iters.length) {
      iters[iters.length - 1].runs.push(r);
    }
  }
  // Iteration-level status: failed if any sub-run failed; running if last is
  // running; otherwise the last run's status. Preserves "1 ci-fix per loop"
  // while letting review/validate observations promote iteration to failed.
  for (const it of iters) {
    const last = it.runs[it.runs.length - 1];
    if (last.status === 'running') it.status = 'running';
    else if (it.runs.some((r) => r.status === 'failed')) it.status = 'failed';
    else if (it.runs.every((r) => r.status === 'completed')) it.status = 'completed';
    else it.status = last.status;
  }
  return iters;
}

/* ─── Task detail bundle ──────────────────────────────────────────── */

export function getTaskDetail(taskId: string): TaskDetail {
  const t = TASKS.find((x) => x.id === taskId) ?? TASKS[0];
  const runs: Run[] = [
    {
      id: 'run_29a1f04b',
      workflow_id: 'wf-quick',
      status: 'completed',
      started_at: minutesAgo(54),
      completed_at: minutesAgo(46),
      duration_s: 480,
      steps: [
        {
          id: 'stp_001',
          role_id: 'plan',
          status: 'completed',
          started_at: minutesAgo(54),
          completed_at: minutesAgo(52),
          duration_s: 121,
          output: { summary: 'scoped 3 callback sites; OAuth + magic-link + SSO' },
          tokens: { in: 4_280, out: 1_120 },
        },
        {
          id: 'stp_002',
          role_id: 'code',
          status: 'completed',
          started_at: minutesAgo(52),
          completed_at: minutesAgo(46),
          duration_s: 360,
          output: {
            summary: '412 LOC across 7 files; preserved sync wrapper for legacy callers',
            commit_sha: 'd12fab9',
          },
          tokens: { in: 18_900, out: 6_720 },
        },
      ],
    },
    {
      id: 'run_b9e84a72',
      workflow_id: 'wf-review',
      status: 'failed',
      started_at: minutesAgo(38),
      completed_at: minutesAgo(14),
      duration_s: 1440,
      steps: [
        {
          id: 'stp_003',
          role_id: 'review',
          status: 'completed',
          started_at: minutesAgo(38),
          completed_at: minutesAgo(31),
          duration_s: 420,
          output: { summary: 'approved with nits on error-coercion' },
          tokens: { in: 14_200, out: 3_810 },
        },
        {
          id: 'stp_004',
          role_id: 'feedback',
          status: 'completed',
          started_at: minutesAgo(31),
          completed_at: minutesAgo(20),
          duration_s: 660,
          output: { summary: 'addressed nits; +3 tests' },
          tokens: { in: 9_120, out: 4_440 },
        },
      ],
    },
    {
      id: 'run_c104e9d3',
      workflow_id: 'wf-ci-fix',
      status: 'running',
      started_at: minutesAgo(14),
      completed_at: null,
      duration_s: null,
      steps: [
        {
          id: 'stp_005',
          role_id: 'ci-analyze',
          status: 'completed',
          started_at: minutesAgo(14),
          completed_at: minutesAgo(13),
          duration_s: 60,
          output: { summary: 'e2e-auth timing out at OAuth handoff (5/5)' },
          tokens: { in: 3_900, out: 880 },
        },
        {
          id: 'stp_006',
          role_id: 'code',
          status: 'failed',
          started_at: minutesAgo(13),
          completed_at: minutesAgo(12),
          duration_s: 60,
          output: null,
          error:
            'Patch reverted by review-bot: introduced async leak in tearDown.\nCannot retry without operator override (>2 ci-fix attempts).',
          tokens: { in: 6_120, out: 2_010 },
        },
      ],
    },
  ];
  return { task: t, runs };
}

const REPO_DOCS: Record<string, RepoDocs> = {
  'osmo/web': {
    arch: '.treadmill/arch.md',
    plans: 3,
    last_updated: minutesAgo(180),
  },
  'osmo/data-pipeline': {
    arch: '.treadmill/arch.md',
    plans: 2,
    last_updated: minutesAgo(1200),
  },
  'treadmill/core': {
    arch: '.treadmill/arch.md',
    plans: 5,
    last_updated: minutesAgo(45),
  },
  'treadmill/dashboard': {
    arch: '.treadmill/arch.md',
    plans: 1,
    last_updated: minutesAgo(15),
  },
};

export function getRepoDocs(repo: string): RepoDocs | null {
  return REPO_DOCS[repo] ?? null;
}

/* ─── Actions — simulated mutations ───────────────────────────────── */

export function cancelTask(taskId: string, reason: string): void {
  const t = TASKS.find((x) => x.id === taskId);
  if (t) {
    t.derived_status = 'cancelled';
    t.escalated = false;
  }
  EVENTS.unshift({
    id: `evt_cancel_${Date.now()}`,
    entity_type: 'task',
    action: 'cancelled',
    task_id: taskId,
    repo: t?.repo ?? null,
    created_at: new Date(),
    detail: `reason: ${reason}`,
  });
}

export function acknowledgeEscalation(taskId: string): void {
  const t = TASKS.find((x) => x.id === taskId);
  if (t) t.escalated = false;
  EVENTS.unshift({
    id: `evt_ack_${Date.now()}`,
    entity_type: 'task',
    action: 'escalation_acknowledged',
    task_id: taskId,
    repo: t?.repo ?? null,
    created_at: new Date(),
    detail: 'by operator',
  });
}

/* ─── Live sim — drives WS-feeling updates ──────────────────────────
 * Internal mutators used by `sim.ts`. Page components do not import
 * these directly; they read via queries.ts.
 */

let SIM_COUNTER = 100;

export function _simAdvance(): { liveTaskId: string | null } {
  // Either bump a live task ("still working") or just emit a scheduler tick.
  const liveTask = TASKS.find((t) =>
    t.pipeline.some((p) => p.status === 'running'),
  );
  if (liveTask && Math.random() < 0.4) {
    const running = liveTask.pipeline.find((p) => p.status === 'running');
    EVENTS.unshift({
      id: `evt_live_${++SIM_COUNTER}`,
      entity_type: 'step',
      action: 'progress',
      task_id: liveTask.id,
      repo: liveTask.repo,
      created_at: new Date(),
      detail: `${running?.role ?? '?'} · ${(liveTask.cost_usd + Math.random() * 0.05).toFixed(2)} USD`,
    });
    liveTask.cost_usd += +(Math.random() * 0.05).toFixed(2);
    liveTask.tokens += Math.floor(Math.random() * 800 + 200);
    liveTask.last_activity = new Date();
    EVENTS = EVENTS.slice(0, 30);
    return { liveTaskId: liveTask.id };
  }
  EVENTS.unshift({
    id: `evt_live_${++SIM_COUNTER}`,
    entity_type: 'schedule',
    action: 'tick',
    task_id: null,
    repo: null,
    created_at: new Date(),
    detail: `scheduler · ${FLEET.workers_running} workers · ${TASKS.length} non-terminal`,
  });
  FLEET.scheduler_last_tick = new Date();
  EVENTS = EVENTS.slice(0, 30);
  return { liveTaskId: null };
}
