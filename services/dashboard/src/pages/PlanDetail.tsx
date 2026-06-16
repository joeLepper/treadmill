/**
 * Plan Detail (/plans/:planId) — the loop view for one plan.
 *
 * Live: /plans/{id} + /plans/{id}/tasks for the task list; each task row
 * fetches its REAL journey (GET /tasks/{id}/journey — executions ⊕ gate
 * events ⊕ cost) and draws the journey bar, expandable to the cycle log.
 * No mock — loading / empty / error. The DOCUMENT tab renders the plan
 * markdown when it's in the bundled doc snapshot.
 */

import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, ChevronDown, ChevronRight } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { DocBody } from '../design/DocBody';
import { JourneyBar, JourneyStats, JourneyLegend, CycleLog } from '../design/Journey';
import { EmptyState } from '../design/States';
import { StepTranscriptDrawer } from '../design/StepTranscript';
import { stepTranscript } from '../api/stepTranscript';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import { useViewState, type ViewState } from '../design/useViewState';
import { usePlanDetail, useTaskJourney, planTitle, planStage, type BoardTask } from '../api/v2queries';
import { DOC_CONTENT } from '../api/docContent';

export function PlanDetail() {
  const { planId } = useParams();
  const navigate = useNavigate();
  const v = useViewState();
  const tab = (v.get('tab', 'execution') === 'document' ? 'document' : 'execution') as 'execution' | 'document';
  const step = stepTranscript(v.get('step'));
  const content = planId ? DOC_CONTENT[planId] : undefined;

  const q = usePlanDetail(planId);
  const data = q.data;
  const title = data?.plan ? planTitle(data.plan.doc_path) : (planId ? planId.slice(0, 8) : 'plan');
  const done = (data?.tasks ?? []).filter((t) => t.stage === 'merged').length;
  const inflight = (data?.tasks ?? []).filter((t) => t.bucket === 'inflight' && t.stage !== 'merged').length;

  return (
    <PageLayout
      title={title}
      breadcrumb={<Crumb onBack={() => navigate('/plans')} />}
      loading={q.isLoading}
      error={q.isError ? (q.error instanceof Error ? q.error : new Error(String(q.error))) : null}
    >
      {step && <StepTranscriptDrawer step={step} onClose={() => v.set('step', null)} />}

      {data && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 1, background: 'var(--tm-border)', border: '1px solid var(--tm-border)', borderRadius: 3, overflow: 'hidden', marginBottom: 16 }}>
            <BandStat label="repo" value={data.plan?.repo ?? '—'} />
            <BandStat label="tasks delivered" value={`${done}/${data.tasks.length}`} tone="ok" />
            <BandStat label="in flight" value={String(inflight)} tone="warn" />
            <BandStat label="stage" value={data.plan ? planStage(data.plan.derived_status) : '—'} tone="info" />
          </div>

          <div style={{ display: 'flex', gap: 4, marginBottom: 14, borderBottom: '1px solid var(--tm-border)' }}>
            {(['execution', 'document'] as const).map((t) => (
              <button key={t} onClick={() => v.set('tab', t, 'execution')}
                style={{ padding: '8px 16px', background: 'transparent', border: 'none', borderBottom: tab === t ? '2px solid var(--tm-warn)' : '2px solid transparent', color: tab === t ? 'var(--tm-t1)' : 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 12, letterSpacing: 0.4, cursor: 'pointer', textTransform: 'uppercase' }}>
                {t}{t === 'document' && !content ? ' ·' : ''}
              </button>
            ))}
          </div>

          {tab === 'execution' ? (
            data.tasks.length === 0 ? (
              <EmptyState message="// no tasks for this plan yet" />
            ) : (
              <>
                <JourneyLegend />
                <Panel padded={false} style={{ overflow: 'visible' }}>
                  {data.tasks.map((t) => <TaskRow key={t.id} t={t} v={v} navigate={navigate} />)}
                </Panel>
              </>
            )
          ) : content ? (
            <Panel padded style={{ background: 'var(--tm-bg)' }}>
              <div style={{ maxWidth: 880 }}><DocBody source={content} /></div>
            </Panel>
          ) : (
            <EmptyState message="// plan document isn't in the bundled snapshot" />
          )}
        </>
      )}
    </PageLayout>
  );
}

function TaskRow({ t, v, navigate }: { t: BoardTask; v: ViewState; navigate: (to: string) => void }) {
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
          <button
            onClick={(e) => { e.stopPropagation(); navigate(`/tasks/${t.id}`); }}
            style={{ marginLeft: 'auto', background: 'transparent', border: 'none', color: 'var(--tm-info-fg)', fontFamily: 'var(--tm-mono)', fontSize: 10, cursor: 'pointer' }}
          >
            open ↗
          </button>
          <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)' }}>{t.worker}{elapsed ? ` · ${fmt.duration(elapsed)}` : ''}</span>
        </div>

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

function BandStat({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div style={{ background: 'var(--tm-surface)', padding: '14px 18px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
      <span className="tm-tnum" style={{ fontSize: 21, fontWeight: 500, color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)', letterSpacing: -0.5 }}>{value}</span>
    </div>
  );
}

function Crumb({ onBack }: { onBack: () => void }) {
  return (
    <button onClick={onBack} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, background: 'transparent', border: 'none', color: 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 10.5, letterSpacing: 0.5, cursor: 'pointer', padding: 0, textTransform: 'uppercase' }}>
      <ArrowLeft size={12} /> plans
    </button>
  );
}
