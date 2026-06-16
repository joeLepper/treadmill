/**
 * Escalations (S6) — kept from v1, vocabulary widened to the ADR-0087
 * classes (evaluator_timeout, rework_exhausted, mergeability_undetermined)
 * plus the op-readiness alert classes when they land (inference_silence,
 * dead_puller). open / ack / closed flow + MTTR, unchanged.
 */

import { useNavigate } from 'react-router-dom';
import { AlertTriangle, BellOff, CheckCircle2 } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { useEscalations } from '../api/v2queries';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import { type EscReason, type Escalation, type EscStatus } from '../api/v2mock';

const REASON_LABEL: Record<EscReason, string> = {
  evaluator_timeout: 'evaluator timeout',
  rework_exhausted: 'rework exhausted',
  mergeability_undetermined: 'mergeability undetermined',
  worker_failure: 'worker failure',
  inference_silence: 'inference silence',
  dead_puller: 'dead puller',
};

/** op-readiness classes are newer + carry a "from alerting" marker. */
const ALERT_CLASS: EscReason[] = ['inference_silence', 'dead_puller'];

const STATUS_TONE: Record<EscStatus, Tone> = { open: 'danger', ack: 'warn', closed: 'muted' };
const STATUS_ICON: Record<EscStatus, typeof AlertTriangle> = {
  open: AlertTriangle,
  ack: BellOff,
  closed: CheckCircle2,
};

export function EscalationsV2() {
  const q = useEscalations();
  // /escalations returns OPEN incidents only (closed/MTTR is a later
  // /escalations/report wire). No mock — loading / empty / error only.
  const escalations: Escalation[] = (q.data?.open ?? []).map((o) => ({
    taskId: o.task_id,
    title: o.title,
    repo: o.repo,
    reason: (o.reason ?? 'worker_failure') as EscReason,
    status: 'open' as EscStatus,
    openedAgeS: Math.max(0, Math.round((Date.now() - Date.parse(o.opened_at)) / 1000)),
  }));

  const open = escalations.filter((e) => e.status === 'open');
  const ack = escalations.filter((e) => e.status === 'ack');
  const closed = escalations.filter((e) => e.status === 'closed');
  const mttrs = closed.filter((e) => e.mttrS != null).map((e) => e.mttrS!);
  const avgMttr = mttrs.length ? mttrs.reduce((a, b) => a + b, 0) / mttrs.length : null;

  return (
    <PageLayout
      title="escalations"
      freshness={<ConnectionAffordance mode="polling" lastUpdated={new Date().toISOString()} />}
      loading={q.isLoading}
      error={q.isError ? (q.error instanceof Error ? q.error : new Error(String(q.error))) : null}
      actions={
        <div style={{ display: 'flex', gap: 22, alignItems: 'center', fontFamily: 'var(--tm-mono)' }}>
          <Stat label="open" value={open.length} tone="danger" />
          <Stat label="acked" value={ack.length} tone="warn" />
          <Stat label="MTTR avg" value={avgMttr != null ? fmt.duration(avgMttr) : '—'} />
        </div>
      }
    >
      {(open.length > 0 || ack.length > 0) ? (
        <Panel accent={open.length > 0 ? 'danger' : 'warn'} title="needs attention" padded={false}>
          {[...open, ...ack].map((e) => <EscRow key={e.taskId} e={e} />)}
        </Panel>
      ) : (
        <Panel title="needs attention" padded>
          <div style={{ color: 'var(--tm-ok-fg)', fontFamily: 'var(--tm-mono)', fontSize: 12 }}>// all clear</div>
        </Panel>
      )}

      <div style={{ height: 16 }} />

      <Panel title="recently closed" padded={false}>
        {closed.map((e) => <EscRow key={e.taskId} e={e} />)}
        {closed.length === 0 && (
          <div style={{ padding: 16, color: 'var(--tm-t4)', fontFamily: 'var(--tm-mono)', fontSize: 12 }}>none</div>
        )}
      </Panel>
    </PageLayout>
  );
}

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: Tone }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
      <span className="tm-tnum" style={{ fontSize: 16, fontWeight: 500, color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)' }}>{value}</span>
      <span style={{ fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
    </div>
  );
}

function EscRow({ e }: { e: Escalation }) {
  const navigate = useNavigate();
  const tone = STATUS_TONE[e.status];
  const Icon = STATUS_ICON[e.status];
  const fromAlert = ALERT_CLASS.includes(e.reason);
  return (
    <div
      onClick={() => navigate(`/tasks/${e.taskId}`)}
      style={{ padding: '12px 16px', borderBottom: '1px solid var(--tm-border)', display: 'flex', alignItems: 'center', gap: 13, cursor: 'pointer' }}
      onMouseEnter={(ev) => (ev.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(ev) => (ev.currentTarget.style.background = 'transparent')}
    >
      <Icon size={15} style={{ color: `var(--tm-${tone}-fg)`, flexShrink: 0 }} />
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontSize: 12.5, color: 'var(--tm-t1)' }}>{e.title}</div>
        <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)', marginTop: 2 }}>
          {e.repo} · {fmt.id(e.taskId, 16)}
        </div>
      </div>

      <span
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 10.5,
          color: `var(--tm-${tone}-fg)`,
          border: `1px solid var(--tm-${tone}-edge)`,
          background: `var(--tm-${tone}-bg)`,
          borderRadius: 4,
          padding: '2px 9px',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          flexShrink: 0,
        }}
      >
        {fromAlert && <span title="raised by an op-readiness alert" style={{ width: 5, height: 5, borderRadius: 999, background: 'var(--tm-info)' }} />}
        {REASON_LABEL[e.reason] ?? e.reason.replace(/_/g, ' ')}
      </span>

      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t3)', width: 90, textAlign: 'right', flexShrink: 0 }}>
        {e.status === 'closed' && e.mttrS != null
          ? `MTTR ${fmt.duration(e.mttrS)}`
          : `open ${fmt.duration(e.openedAgeS)}`}
      </span>
    </div>
  );
}
