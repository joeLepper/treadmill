/**
 * Journey — the shared loop-cycle visual language.
 *
 * One task's life is a sequence of cycles (dispatch → CI → review → rework
 * → eval → merge), each with an outcome, a duration, a cost, and sometimes
 * a captured transcript. This module owns how those cycles are drawn so the
 * SAME treatment appears everywhere a loop shows up: the plan-detail loop
 * view, the loop/tasks board, and a worker's contributions on its task.
 *
 *  - <JourneyBar>   — compact proportional timeline (width = time in cycle,
 *                     color = outcome), with a per-segment hover popover.
 *  - <JourneyStats> — the stage tallies (elapsed / CI runs / review rounds /
 *                     rework cycles / cost).
 *  - <CycleLog>     — the vertical cycle log; cycles with a transcript get a
 *                     "view conversation" affordance (onOpenStep).
 *
 * Encoding is documented per DESIGN.md Convention 2 — callers pair the bar
 * with a <JourneyLegend> (also exported) so the chart self-documents.
 */

import { useState } from 'react';
import {
  CircleCheck, CircleX, GitMerge, MessagesSquare, Radio,
  ShieldCheck, Users,
} from 'lucide-react';
import { fmt } from './fmt';
import type { Tone } from './fmt';
import { stepTranscript } from '../api/stepTranscript';
import type { CycleKind, CycleOutcome, TaskCycle } from '../api/v2mock';

export const OUTCOME_TONE: Record<CycleOutcome, Tone> = {
  pass: 'ok', lgtm: 'ok', approve: 'ok', merged: 'ok',
  fail: 'danger', changes: 'danger',
  rework: 'warn', running: 'warn', backfill: 'muted',
};

export const KIND_ICON: Record<CycleKind, typeof Radio> = {
  dispatch: Radio, ci: CircleCheck, review: Users, eval: ShieldCheck, merge: GitMerge,
};

export function journeyTotals(cycles: TaskCycle[]) {
  const ci = cycles.filter((c) => c.kind === 'ci');
  const reviews = cycles.filter((c) => c.kind === 'review' && (c.outcome === 'lgtm' || c.outcome === 'changes'));
  const reworks = cycles.filter((c) => c.outcome === 'rework');
  return {
    elapsed: cycles.reduce((n, c) => n + c.durationS, 0),
    cost: cycles.reduce((n, c) => n + (c.costUsd ?? 0), 0),
    ciRuns: ci.length,
    ciFails: ci.filter((c) => c.outcome === 'fail').length,
    reviewRounds: reviews.length,
    reviewsBack: reviews.filter((c) => c.outcome === 'changes').length,
    reworks: reworks.length,
    reworkCost: reworks.reduce((n, c) => n + (c.costUsd ?? 0), 0),
  };
}

/** States the journey-bar encoding so it self-documents (Convention 2). */
export function JourneyLegend() {
  const keys: { tone: Tone; label: string }[] = [
    { tone: 'ok', label: 'pass' },
    { tone: 'warn', label: 'rework' },
    { tone: 'danger', label: 'fail' },
    { tone: 'muted', label: 'backfill' },
  ];
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '7px 4px 9px', flexWrap: 'wrap', fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>
      <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <span style={{ display: 'inline-block', width: 22, height: 8, borderRadius: 1, background: 'linear-gradient(90deg, var(--tm-ok-bg), var(--tm-ok-edge))', border: '1px solid var(--tm-border)' }} />
        bar width = time in cycle
      </span>
      <span style={{ color: 'var(--tm-t4)' }}>·</span>
      {keys.map((k) => (
        <span key={k.label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span style={{ width: 9, height: 9, borderRadius: 2, background: `var(--tm-${k.tone}-bg)`, border: `1px solid var(--tm-${k.tone}-edge)` }} />
          {k.label}
        </span>
      ))}
      <span style={{ marginLeft: 'auto', color: 'var(--tm-t4)' }}>hover a segment for detail · click a row for the full timeline</span>
    </div>
  );
}

/** Compact horizontal journey — one segment per real cycle. Width = time,
 *  color = outcome; loops surface as repeated CI/review segments. */
export function JourneyBar({ cycles }: { cycles: TaskCycle[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const total = cycles.reduce((n, c) => n + Math.max(c.durationS, 60), 0);
  return (
    <div style={{ display: 'flex', gap: 2, alignItems: 'center', height: 18, position: 'relative' }}>
      {cycles.map((c, i) => {
        const tone = OUTCOME_TONE[c.outcome];
        const w = (Math.max(c.durationS, 60) / total) * 100;
        const bad = c.outcome === 'fail' || c.outcome === 'changes';
        return (
          <div
            key={i}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover((h) => (h === i ? null : h))}
            style={{ width: `${w}%`, minWidth: 7, height: '100%', background: `var(--tm-${tone}-bg)`, border: `1px solid ${hover === i ? `var(--tm-${tone}-fg)` : `var(--tm-${tone}-edge)`}`, borderRadius: 2, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            {bad && <CircleX size={10} style={{ color: `var(--tm-${tone}-fg)` }} />}
          </div>
        );
      })}
      {hover != null && (() => {
        const c = cycles[hover];
        const tone = OUTCOME_TONE[c.outcome];
        const Icon = KIND_ICON[c.kind];
        const pct = Math.round((Math.max(c.durationS, 60) / total) * 100);
        return (
          <div style={{ position: 'absolute', bottom: 'calc(100% + 5px)', left: 0, zIndex: 30, background: 'var(--tm-surface-2)', border: `1px solid var(--tm-${tone}-edge)`, borderRadius: 3, padding: '7px 9px', minWidth: 168, boxShadow: '0 6px 22px rgba(0,0,0,0.45)', pointerEvents: 'none' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <Icon size={11} style={{ color: `var(--tm-${tone}-fg)` }} />
              <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t1)' }}>{c.label}</span>
              <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: `var(--tm-${tone}-fg)`, textTransform: 'uppercase', letterSpacing: 0.4, marginLeft: 'auto' }}>{c.outcome}</span>
            </div>
            <div style={{ display: 'flex', gap: 12, marginTop: 5, fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-t4)' }}>
              <span><span style={{ color: 'var(--tm-t2)' }}>{fmt.duration(c.durationS)}</span> · {pct}% of run</span>
              {c.costUsd != null && <span style={{ color: 'var(--tm-info-fg)' }}>{fmt.usd(c.costUsd)}</span>}
            </div>
            {c.detail && <div style={{ fontSize: 10.5, color: 'var(--tm-t3)', marginTop: 5, lineHeight: 1.4 }}>{c.detail}</div>}
          </div>
        );
      })()}
    </div>
  );
}

export function JourneyStats({ cycles }: { cycles: TaskCycle[] }) {
  const t = journeyTotals(cycles);
  return (
    <div style={{ display: 'flex', gap: 22, padding: '10px 0 14px', flexWrap: 'wrap' }}>
      <JStat label="elapsed active" value={fmt.duration(t.elapsed)} />
      <JStat label="CI runs" value={`${t.ciRuns}`} tone={t.ciFails ? 'warn' : 'ok'} sub={`${t.ciFails} failed`} />
      <JStat label="review rounds" value={`${t.reviewRounds}`} tone="info" sub={`${t.reviewsBack} sent back`} />
      <JStat label="rework cycles" value={`${t.reworks}`} tone={t.reworks ? 'warn' : undefined} sub={fmt.usd(t.reworkCost)} />
      <JStat label="cost" value={fmt.usd(t.cost)} tone="info" />
    </div>
  );
}

function JStat({ label, value, tone, sub }: { label: string; value: string; tone?: Tone; sub?: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9, letterSpacing: 0.5, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
      <span className="tm-tnum" style={{ fontSize: 17, fontWeight: 500, color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)' }}>{value}</span>
      {sub && <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-t4)' }}>{sub}</span>}
    </div>
  );
}

/** Vertical cycle log — every dispatch / CI run / review / rework / verdict /
 *  merge with actor, duration, cost, detail. Cycles with a captured
 *  transcript expose a "view conversation" affordance via onOpenStep. */
export function CycleLog({ cycles, onOpenStep }: { cycles: TaskCycle[]; onOpenStep?: (stepId: string) => void }) {
  return (
    <div style={{ position: 'relative', paddingLeft: 4 }}>
      {cycles.map((c, i) => {
        const tone = OUTCOME_TONE[c.outcome];
        const Icon = KIND_ICON[c.kind];
        const last = i === cycles.length - 1;
        const transcript = stepTranscript(c.stepId);
        const clickable = !!transcript && !!onOpenStep;
        return (
          <div key={i} style={{ display: 'flex', gap: 12, position: 'relative' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0 }}>
              <div style={{ width: 22, height: 22, borderRadius: 999, background: `var(--tm-${tone}-bg)`, border: `1px solid var(--tm-${tone}-edge)`, display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1 }}>
                <Icon size={11} style={{ color: `var(--tm-${tone}-fg)` }} />
              </div>
              {!last && <div style={{ width: 1, flex: 1, minHeight: 14, background: 'var(--tm-border-2)' }} />}
            </div>
            <div
              onClick={clickable ? () => onOpenStep!(c.stepId!) : undefined}
              style={{ paddingBottom: 14, minWidth: 0, flex: 1, cursor: clickable ? 'pointer' : 'default', borderRadius: 3, margin: '0 -8px', padding: '2px 8px 12px' }}
              onMouseEnter={(e) => clickable && (e.currentTarget.style.background = 'var(--tm-hover)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 9, flexWrap: 'wrap' }}>
                <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11.5, color: 'var(--tm-t1)' }}>{c.label}</span>
                <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: `var(--tm-${tone}-fg)`, textTransform: 'uppercase', letterSpacing: 0.4 }}>{c.outcome}</span>
                <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>{c.actor}</span>
                {c.durationS > 0 && <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>{fmt.duration(c.durationS)}</span>}
                {c.costUsd != null && <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-info-fg)', marginLeft: 'auto' }}>{fmt.usd(c.costUsd)}</span>}
              </div>
              {c.detail && <div style={{ fontSize: 11.5, color: 'var(--tm-t3)', marginTop: 3, lineHeight: 1.45 }}>{c.detail}</div>}
              {clickable && (
                <div style={{ display: 'inline-flex', alignItems: 'center', gap: 5, marginTop: 6, fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-info-fg)', border: '1px solid var(--tm-info-edge)', background: 'var(--tm-info-bg)', borderRadius: 3, padding: '2px 7px' }}>
                  <MessagesSquare size={10} /> view conversation · {transcript!.turnCount} turns
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
