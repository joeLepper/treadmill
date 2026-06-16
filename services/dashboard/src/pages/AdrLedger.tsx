/**
 * ADR Ledger — architectural DECISIONS (split from plans).
 *
 * An ADR is a decision, not a unit of work: no tasks, no execution metrics.
 * The ledger surfaces every ADR across both repos with its position in the
 * intent pipeline (draft → review → merged) + owner / reviewer; drill into
 * the doc reader to read the decision itself.
 */

import { useNavigate } from 'react-router-dom';
import { FileText, GitBranch, ScrollText } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import { INTENT_STAGES, type IntentStage, type LedgerDoc } from '../api/v2mock';
import { realLedger } from '../api/docContent';

const STAGE_TONE: Record<IntentStage, Tone> = {
  draft: 'muted', review: 'info', 'pr-open': 'warn', merged: 'ok', submitted: 'info', executing: 'warn', done: 'ok',
};
const STAGE_LABEL: Record<IntentStage, string> = {
  draft: 'draft', review: 'review', 'pr-open': 'PR open', merged: 'merged', submitted: 'submitted', executing: 'executing', done: 'done',
};

export function AdrLedger() {
  const adrs = realLedger.filter((d) => d.kind === 'ADR');
  const byStage = (s: IntentStage) => adrs.filter((d) => d.stage === s).length;
  const STAGES_SHOWN: IntentStage[] = ['draft', 'review', 'pr-open', 'merged'];

  return (
    <PageLayout
      title="decisions"
      breadcrumb={<span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)', letterSpacing: 0.5 }}>ADR LEDGER</span>}
      freshness={<ConnectionAffordance mode="polling" lastUpdated={new Date().toISOString()} />}
      actions={
        <div style={{ display: 'flex', gap: 18 }}>
          {STAGES_SHOWN.map((s) => (
            <div key={s} style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
              <span className="tm-tnum" style={{ fontSize: 16, fontWeight: 500, color: byStage(s) ? `var(--tm-${STAGE_TONE[s]}-fg)` : 'var(--tm-t4)' }}>{byStage(s)}</span>
              <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9, letterSpacing: 0.5, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{STAGE_LABEL[s]}</span>
            </div>
          ))}
        </div>
      }
    >
      <Panel padded={false}>
        {adrs.map((d) => <AdrRow key={d.id} d={d} />)}
      </Panel>
    </PageLayout>
  );
}

function AdrRow({ d }: { d: LedgerDoc }) {
  const navigate = useNavigate();
  const tone = STAGE_TONE[d.stage];
  const idx = INTENT_STAGES.indexOf(d.stage);
  return (
    <div
      onClick={() => navigate(`/adrs/${d.id}`)}
      style={{ padding: '13px 16px', borderBottom: '1px solid var(--tm-border)', display: 'flex', alignItems: 'center', gap: 14, cursor: 'pointer' }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <ScrollText size={16} style={{ color: 'var(--tm-info-fg)', flexShrink: 0 }} />
      <div style={{ minWidth: 0, flex: '0 1 360px' }}>
        <div style={{ fontSize: 13, color: 'var(--tm-t1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.title}</div>
        <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)', display: 'flex', gap: 8, marginTop: 2 }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}><FileText size={10} />{d.owner}</span>
          <span>rev: {d.reviewer}</span>
        </div>
      </div>
      {/* Labeled pipeline stepper — self-documenting in place (DESIGN.md
          Convention 2): the stage names ARE the legend, the fill shows
          how far along. Past = green, current = stage tone, upcoming = ghost. */}
      <div style={{ display: 'flex', gap: 4, flex: 1, justifyContent: 'center', alignItems: 'center' }}>
        {INTENT_STAGES.slice(0, 4).map((s, i) => {
          const done = i < idx;
          const cur = i === idx;
          const st: Tone | 'ghost' = done ? 'ok' : cur ? tone : 'ghost';
          const bg = st === 'ghost' ? 'transparent' : `var(--tm-${st}-bg)`;
          const fg = st === 'ghost' ? 'var(--tm-t4)' : `var(--tm-${st}-fg)`;
          const edge = st === 'ghost' ? 'var(--tm-border)' : `var(--tm-${st}-edge)`;
          return (
            <span
              key={s}
              title={done ? `${STAGE_LABEL[s]} — done` : cur ? `${STAGE_LABEL[s]} — current stage` : `${STAGE_LABEL[s]} — not yet`}
              style={{
                fontFamily: 'var(--tm-mono)', fontSize: 9, letterSpacing: 0.2,
                color: fg, background: bg, border: `1px solid ${edge}`, borderRadius: 3,
                padding: '2px 7px', whiteSpace: 'nowrap',
                opacity: st === 'ghost' ? 0.55 : 1,
                fontWeight: cur ? 600 : 400,
              }}
            >
              {STAGE_LABEL[s]}
            </span>
          );
        })}
      </div>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: `var(--tm-${tone}-fg)`, background: `var(--tm-${tone}-bg)`, border: `1px solid var(--tm-${tone}-edge)`, borderRadius: 4, padding: '2px 8px', flexShrink: 0, width: 84, textAlign: 'center' }}>
        {STAGE_LABEL[d.stage]}
      </span>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)', width: 64, textAlign: 'right', flexShrink: 0 }}>
        {d.prNumber ? <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, color: 'var(--tm-info-fg)' }}><GitBranch size={10} />#{d.prNumber}</span> : fmt.duration(d.updatedAgeS)}
      </span>
    </div>
  );
}
