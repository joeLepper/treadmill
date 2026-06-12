/**
 * Plan Ledger — units of WORK (split from ADRs).
 *
 * Live: derived from /api/v1/tasks grouped by plan_id + /plans/{id} for
 * each plan's repo / status / doc-title. Per-plan cost isn't served yet,
 * so it shows "—" under live (no fake number). Falls back to the mock
 * plan records when the API is unreachable. Drill into the plan detail
 * (the loop view) for the per-task execution.
 */

import { useNavigate } from 'react-router-dom';
import { Layers } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import { usePlans } from '../api/v2queries';
import { Loading, EmptyState, ErrorState } from '../design/States';
import { type IntentStage } from '../api/v2mock';

const STAGE_TONE: Record<IntentStage, Tone> = {
  draft: 'muted', review: 'info', 'pr-open': 'warn', merged: 'ok', submitted: 'info', executing: 'warn', done: 'ok',
};
const STAGE_LABEL: Record<IntentStage, string> = {
  draft: 'draft', review: 'review', 'pr-open': 'PR open', merged: 'merged', submitted: 'submitted', executing: 'executing', done: 'done',
};

interface LedgerRow {
  id: string; title: string; repo: string; stage: IntentStage;
  tasksTotal: number; tasksDone: number; cost: number | null;
}

export function PlanLedger() {
  const navigate = useNavigate();
  const q = usePlans();
  const rows: LedgerRow[] = (q.data?.plans ?? []).map((p) => ({ id: p.id, title: p.title, repo: p.repo, stage: p.stage, tasksTotal: p.tasksTotal, tasksDone: p.tasksDone, cost: null }));
  const totalTasks = rows.reduce((n, r) => n + r.tasksTotal, 0);
  const doneTasks = rows.reduce((n, r) => n + r.tasksDone, 0);

  return (
    <PageLayout
      title="plans"
      breadcrumb={<span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)', letterSpacing: 0.5 }}>WORK LEDGER</span>}
      freshness={<ConnectionAffordance mode="polling" lastUpdated={new Date().toISOString()} />}
      actions={
        <div style={{ display: 'flex', gap: 22, alignItems: 'center' }}>
          <Stat label="plans" value={String(rows.length)} />
          <Stat label="tasks done" value={`${doneTasks}/${totalTasks}`} tone="ok" />
        </div>
      }
    >
      {q.isLoading ? (
        <Loading label="loading plans" />
      ) : q.isError ? (
        <ErrorState error={q.error} what="the plans ledger" />
      ) : rows.length === 0 ? (
        <EmptyState message="// no plans" />
      ) : (
        <Panel padded={false}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 130px 120px 92px', gap: 12, padding: '8px 16px', borderBottom: '1px solid var(--tm-border)', fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.5, textTransform: 'uppercase', color: 'var(--tm-t4)', background: 'var(--tm-surface)' }}>
            <span>plan</span>
            <span>tasks</span>
            <span style={{ textAlign: 'right' }}>cost</span>
            <span style={{ textAlign: 'center' }}>stage</span>
          </div>
          {rows.map((p) => <PlanRow key={p.id} p={p} onClick={() => navigate(`/plans/${p.id}`)} />)}
        </Panel>
      )}
    </PageLayout>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
      <span className="tm-tnum" style={{ fontSize: 16, fontWeight: 500, color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)' }}>{value}</span>
      <span style={{ fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)', fontFamily: 'var(--tm-mono)' }}>{label}</span>
    </div>
  );
}

function PlanRow({ p, onClick }: { p: LedgerRow; onClick: () => void }) {
  const tone = STAGE_TONE[p.stage];
  const pct = p.tasksTotal ? p.tasksDone / p.tasksTotal : 0;
  return (
    <div
      onClick={onClick}
      style={{ display: 'grid', gridTemplateColumns: '1fr 130px 120px 92px', gap: 12, alignItems: 'center', padding: '13px 16px', borderBottom: '1px solid var(--tm-border)', cursor: 'pointer' }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <div style={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 11 }}>
        <Layers size={15} style={{ color: 'var(--tm-warn-fg)', flexShrink: 0 }} />
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, color: 'var(--tm-t1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.title}</div>
          <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)', marginTop: 2 }}>{p.repo}</div>
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t2)' }}>{p.tasksDone}/{p.tasksTotal}</span>
        <div style={{ height: 4, background: 'var(--tm-surface-2)', borderRadius: 999, overflow: 'hidden' }}>
          <div style={{ width: `${pct * 100}%`, height: '100%', background: pct === 1 ? 'var(--tm-ok)' : 'var(--tm-warn)' }} />
        </div>
      </div>

      <div style={{ textAlign: 'right', fontFamily: 'var(--tm-mono)', fontSize: 12, color: p.cost != null ? 'var(--tm-t1)' : 'var(--tm-t4)' }}>
        {p.cost != null ? fmt.usd(p.cost) : '—'}
      </div>

      <span style={{ justifySelf: 'center', fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: `var(--tm-${tone}-fg)`, background: `var(--tm-${tone}-bg)`, border: `1px solid var(--tm-${tone}-edge)`, borderRadius: 4, padding: '2px 8px', width: 84, textAlign: 'center' }}>
        {STAGE_LABEL[p.stage]}
      </span>
    </div>
  );
}
