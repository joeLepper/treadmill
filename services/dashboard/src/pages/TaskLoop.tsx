/**
 * Task loop detail (/tasks/:taskId) — one task's loop, on its own page.
 *
 * The journey is real: GET /tasks/{id}/journey merges task_executions with
 * gate events (CI / review / eval / merge) and attributes token cost. No
 * mock fallback — loading / empty / error are the only non-data states.
 */

import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { JourneyStats, CycleLog } from '../design/Journey';
import { Loading, EmptyState, ErrorState } from '../design/States';
import { StepTranscriptDrawer } from '../design/StepTranscript';
import { stepTranscript } from '../api/stepTranscript';
import { useViewState } from '../design/useViewState';
import { useTasks, useTaskJourney } from '../api/v2queries';

export function TaskLoop() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const v = useViewState();
  const step = stepTranscript(v.get('step'));

  const tasks = useTasks();
  const task = taskId ? tasks.data?.tasks.find((t) => t.id === taskId) : undefined;
  const journey = useTaskJourney(taskId);

  return (
    <PageLayout
      title={task?.title ?? (taskId ? taskId.slice(0, 8) : 'task')}
      breadcrumb={
        <button onClick={() => navigate('/tasks')} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, background: 'transparent', border: 'none', color: 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 10.5, letterSpacing: 0.5, cursor: 'pointer', padding: 0, textTransform: 'uppercase' }}>
          <ArrowLeft size={12} /> tasks
        </button>
      }
    >
      {step && <StepTranscriptDrawer step={step} onClose={() => v.set('step', null)} />}

      {task && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginBottom: 16, padding: '12px 16px', border: '1px solid var(--tm-border)', borderRadius: 3, background: 'var(--tm-surface)', flexWrap: 'wrap' }}>
          <Meta label="repo" value={task.repo} mono />
          <Meta label="worker" value={task.worker} mono />
          <Meta label="stage" value={task.statusLabel} mono />
          {task.planId && (
            <button onClick={() => navigate(`/plans/${task.planId}?task=${taskId}`)}
              style={{ marginLeft: 'auto', background: 'transparent', border: '1px solid var(--tm-border-2)', color: 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 10.5, padding: '4px 9px', borderRadius: 2, cursor: 'pointer' }}>
              in plan ↗
            </button>
          )}
        </div>
      )}

      <Panel padded title="loop journey">
        {journey.isLoading ? (
          <Loading label="loading journey" />
        ) : journey.isError ? (
          <ErrorState error={journey.error} what="the task journey" />
        ) : !journey.data || journey.data.length === 0 ? (
          <EmptyState message="// no cycles recorded for this task yet" />
        ) : (
          <>
            <JourneyStats cycles={journey.data} />
            <div style={{ marginTop: 6 }}>
              <CycleLog cycles={journey.data} onOpenStep={(id) => v.set('step', id)} />
            </div>
          </>
        )}
      </Panel>
    </PageLayout>
  );
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
      <span style={{ fontSize: 12, color: 'var(--tm-t1)', fontFamily: mono ? 'var(--tm-mono)' : 'var(--tm-sans)' }}>{value}</span>
    </div>
  );
}
