/**
 * Tasks board (/tasks) — non-terminal tasks, each with its REAL loop journey.
 *
 * Tasks come from /dashboard/overview; each row fetches its own journey
 * (GET /tasks/{id}/journey — executions ⊕ gate events ⊕ cost) and draws the
 * journey bar (segment width = time in cycle, color = outcome). Click a row
 * to expand the full cycle log. No mock fallback — loading / empty / error.
 */

import { ChevronDown, ChevronRight } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { StepTranscriptDrawer } from '../design/StepTranscript';
import { stepTranscript } from '../api/stepTranscript';
import { JourneyBar, JourneyStats, JourneyLegend, CycleLog } from '../design/Journey';
import { Loading, EmptyState, ErrorState } from '../design/States';
import { useViewState, type ViewState } from '../design/useViewState';
import { useOverview, useTaskJourney, parseStatus } from '../api/v2queries';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import type { Bucket } from '../api/v2mock';

const BUCKETS: { key: Bucket | 'all'; label: string; tone: Tone }[] = [
  { key: 'all', label: 'all', tone: 'info' },
  { key: 'blocked', label: 'blocked', tone: 'danger' },
  { key: 'inflight', label: 'in flight', tone: 'warn' },
  { key: 'hopper', label: 'hopper', tone: 'muted' },
];

interface BoardRow { id: string; title: string; repo: string; worker: string; bucket: Bucket; statusLabel: string; ageS: number; }

export function LoopPipeline() {
  const v = useViewState();
  const bucket = (v.get('bucket', 'all') ?? 'all') as Bucket | 'all';
  const step = stepTranscript(v.get('step'));
  const overview = useOverview();

  const now = Date.now();
  const all: BoardRow[] = (overview.data?.tasks ?? []).map((t) => {
    const p = parseStatus(t.derived_status);
    const start = t.last_activity ?? t.started_at ?? t.created_at;
    return { id: t.id, title: t.title, repo: t.repo, worker: p.worker, bucket: p.bucket, statusLabel: p.label, ageS: Math.max(0, Math.round((now - Date.parse(start)) / 1000)) };
  });
  const rows = bucket === 'all' ? all : all.filter((t) => t.bucket === bucket);
  const count = (b: Bucket) => all.filter((t) => t.bucket === b).length;

  return (
    <PageLayout
      title="tasks"
      freshness={<ConnectionAffordance mode="polling" lastUpdated={new Date().toISOString()} />}
    >
      {step && <StepTranscriptDrawer step={step} onClose={() => v.set('step', null)} />}
      <div style={{ display: 'grid', gridTemplateColumns: '150px minmax(0,1fr)', gap: 16, alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {BUCKETS.map((b) => {
            const active = bucket === b.key;
            const n = b.key === 'all' ? all.length : count(b.key as Bucket);
            return (
              <button key={b.key} onClick={() => v.set('bucket', b.key, 'all')}
                style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '9px 11px', borderRadius: 2, border: `1px solid ${active ? `var(--tm-${b.tone}-edge)` : 'var(--tm-border)'}`, background: active ? `var(--tm-${b.tone}-bg)` : 'transparent', color: active ? `var(--tm-${b.tone}-fg)` : 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 12, cursor: 'pointer', letterSpacing: 0.3 }}>
                <span>{b.label}</span>
                <span className="tm-tnum" style={{ color: active ? `var(--tm-${b.tone}-fg)` : 'var(--tm-t4)' }}>{n}</span>
              </button>
            );
          })}
        </div>

        <div>
          <JourneyLegend />
          {overview.isLoading ? (
            <Loading label="loading tasks" />
          ) : overview.isError ? (
            <ErrorState error={overview.error} what="the task board" />
          ) : (
            <Panel padded={false} style={{ overflow: 'visible' }}>
              {rows.map((t) => <TaskRow key={t.id} t={t} v={v} />)}
              {rows.length === 0 && <EmptyState message="no tasks in this bucket" />}
            </Panel>
          )}
        </div>
      </div>
    </PageLayout>
  );
}

function TaskRow({ t, v }: { t: BoardRow; v: ViewState }) {
  const open = v.is('task', t.id);
  const journey = useTaskJourney(t.id);
  const cycles = journey.data ?? [];
  const hasCycles = cycles.length > 0;
  const elapsed = cycles.reduce((n, c) => n + c.durationS, 0);

  return (
    <div style={{ borderBottom: '1px solid var(--tm-border)' }}>
      <div
        onClick={() => hasCycles && v.toggle('task', t.id)}
        style={{ padding: '13px 16px', display: 'flex', flexDirection: 'column', gap: 9, cursor: hasCycles ? 'pointer' : 'default' }}
        onMouseEnter={(e) => hasCycles && (e.currentTarget.style.background = 'var(--tm-hover)')}
        onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
      >
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          {hasCycles && <span style={{ color: 'var(--tm-t4)', alignSelf: 'center' }}>{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>}
          <span style={{ fontSize: 13, color: 'var(--tm-t1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.title}</span>
          <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-t4)', border: '1px solid var(--tm-border)', borderRadius: 3, padding: '0 5px' }}>{t.statusLabel}</span>
          <span style={{ marginLeft: 'auto', fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)' }}>
            {t.worker} · {fmt.duration(elapsed || t.ageS)}{elapsed ? ' active' : ''}
          </span>
        </div>

        {/* the journey bar — real cycles; loading shimmer while fetching */}
        {journey.isLoading ? (
          <div style={{ height: 18, background: 'var(--tm-surface)', borderRadius: 2, animation: 'tm-pulse-soft 1.6s ease-in-out infinite' }} />
        ) : hasCycles ? (
          <JourneyBar cycles={cycles} />
        ) : (
          <div style={{ height: 18, display: 'flex', alignItems: 'center', fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>no cycles yet</div>
        )}
      </div>

      {open && hasCycles && (
        <div style={{ padding: '6px 16px 18px 40px', background: 'var(--tm-bg)' }}>
          <JourneyStats cycles={cycles} />
          <CycleLog cycles={cycles} onOpenStep={(id) => v.set('step', id)} />
        </div>
      )}
    </div>
  );
}
