/**
 * <WorkerDrawer> — drill into one session (worker / coordinator / evaluator).
 *
 * Answers the operator's "what is this worker, and what has it done?":
 * identity + liveness, what it specializes in, the task it's on now, and —
 * the heart — its step-wise CONTRIBUTIONS on that task, drawn with the same
 * journey language as the plan-detail loop (JourneyStats + CycleLog). Steps
 * with a captured transcript drill all the way into the conversation.
 *
 * Opened from a Mission Control session tile; URL-addressable (?session=).
 */

import type { ReactNode } from 'react';
import { X, CircleDot } from 'lucide-react';
import { fmt } from './fmt';
import type { Tone } from './fmt';
import { JourneyStats, CycleLog } from './Journey';
import { sessionModel, type Session, type TaskCycle } from '../api/v2mock';

const STATE_TONE: Record<Session['state'], Tone> = { live: 'ok', idle: 'muted', down: 'danger' };

export function WorkerDrawer({
  session, liveCycles, onClose, onOpenStep,
}: {
  session: Session;
  /** Live journey for the worker's current task. */
  liveCycles?: TaskCycle[];
  onClose: () => void;
  onOpenStep: (stepId: string) => void;
}) {
  const contributions = liveCycles && liveCycles.length > 0 ? liveCycles : undefined;
  const tone = STATE_TONE[session.state];
  const live = session.current.filter((c) => !c.awaitingMerge);
  const awaiting = session.current.filter((c) => c.awaitingMerge);
  return (
    <div
      onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 40, display: 'flex', justifyContent: 'flex-end' }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="tm-scroll"
        style={{ width: 'min(560px, 92vw)', height: '100%', background: 'var(--tm-bg)', borderLeft: '1px solid var(--tm-border)', overflow: 'auto', boxShadow: '-24px 0 60px rgba(0,0,0,0.45)' }}
      >
        {/* header */}
        <div style={{ position: 'sticky', top: 0, background: 'var(--tm-bg)', borderBottom: '1px solid var(--tm-border)', padding: '14px 18px', zIndex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{session.role}</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 3 }}>
                <span style={{ width: 8, height: 8, borderRadius: 999, background: `var(--tm-${tone})`, animation: session.state === 'live' ? 'tm-pulse-soft 1.8s ease-in-out infinite' : 'none' }} />
                <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 14, color: 'var(--tm-t1)' }}>{session.label}</span>
              </div>
            </div>
            <button onClick={onClose} style={{ background: 'transparent', border: '1px solid var(--tm-border)', borderRadius: 2, color: 'var(--tm-t3)', cursor: 'pointer', padding: 4, display: 'flex' }}>
              <X size={14} />
            </button>
          </div>
          <div style={{ display: 'flex', gap: 16, marginTop: 11, flexWrap: 'wrap' }}>
            <Fact label="state" value={session.state} tone={tone} />
            <Fact label="model" value={sessionModel(session)} />
            <Fact label="last seen" value={fmt.duration(session.lastEventAgeS)} />
            <Fact label="today" value={`${session.today.initial}·${session.today.rework}·${session.today.review}`} sub="init·rework·review" />
          </div>
          {session.specialty && (
            <div style={{ marginTop: 11, fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t3)' }}>
              <span style={{ color: 'var(--tm-t4)' }}>specializes in </span>
              <span style={{ color: 'var(--tm-info-fg)' }}>{session.specialty}</span>
            </div>
          )}
        </div>

        <div style={{ padding: '14px 18px 40px' }}>
          {/* current work */}
          <Section title="on now">
            {live.length === 0 && awaiting.length === 0 ? (
              <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 11.5, color: 'var(--tm-t4)', fontStyle: 'italic' }}>
                {session.role === 'coordinator' ? 'routing — no task of its own' : session.role === 'evaluator' ? 'watching for verdicts' : 'idle'}
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {live.map((c) => (
                  <div key={c.taskId} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <span style={{ fontSize: 12.5, color: 'var(--tm-t1)' }}>{c.title}</span>
                    <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>{c.trigger} · {fmt.duration(c.startedAgeS)}</span>
                  </div>
                ))}
                {awaiting.map((c) => (
                  <div key={c.taskId} style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                    <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-info-fg)', border: '1px solid var(--tm-info-edge)', background: 'var(--tm-info-bg)', borderRadius: 3, padding: '1px 5px' }}>awaiting merge</span>
                    <span style={{ fontSize: 11.5, color: 'var(--tm-t3)' }}>{c.title}</span>
                  </div>
                ))}
              </div>
            )}
          </Section>

          {/* the heart — step-wise contributions on the current task (live) */}
          {contributions && (
            <Section title="contributions on this task">
              <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)', lineHeight: 1.4, marginBottom: 10 }}>
                its current task — executions ⊕ gate cycles
              </div>
              <JourneyStats cycles={contributions} />
              <div style={{ marginTop: 6 }}>
                <CycleLog cycles={contributions} onOpenStep={onOpenStep} />
              </div>
            </Section>
          )}

          {!contributions && (
            <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t4)', lineHeight: 1.5, marginTop: 8 }}>
              <span style={{ color: 'var(--tm-t3)' }}>// no in-flight task to detail for this session.</span>
              <br />
              Contributions are reconstructed from the session's task_executions; this one
              {session.role === 'coordinator' ? ' coordinates rather than executes (no worker steps of its own).' : session.role === 'evaluator' ? ' renders verdicts rather than executing task steps.' : ' has no in-flight task right now.'}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 10 }}>
        <CircleDot size={11} style={{ color: 'var(--tm-t4)' }} />
        <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t3)' }}>{title}</span>
      </div>
      {children}
    </div>
  );
}

function Fact({ label, value, tone, sub }: { label: string; value: string; tone?: Tone; sub?: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 8.5, letterSpacing: 0.5, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
      <span style={{ fontSize: 13, color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)', fontFamily: 'var(--tm-mono)' }}>{value}</span>
      {sub && <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 8.5, color: 'var(--tm-t4)' }}>{sub}</span>}
    </div>
  );
}
