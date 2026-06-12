/**
 * Drafts Ledger (S4) — the pipeline of intent (Joe directive 2026-06-11).
 *
 * Every plan + ADR across both repos with its position:
 * draft → review → PR open → merged → submitted → executing → done,
 * plus owner + reviewer. The week's lived gap: specced work was invisible
 * outside relay logs (an ADR on a branch, a plan uncommitted in a
 * worktree, two doc PRs idle for days). This is the surface upstream of
 * the task board.
 *
 * Sources: docs frontmatter Status, open PRs on docs paths, the plans
 * table, the roadmap boulder index.
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FileText, GitBranch } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import { INTENT_STAGES, ledger, type IntentStage, type LedgerDoc } from '../api/v2mock';

const STAGE_TONE: Record<IntentStage, Tone> = {
  draft: 'muted',
  review: 'info',
  'pr-open': 'warn',
  merged: 'ok',
  submitted: 'info',
  executing: 'warn',
  done: 'ok',
};

const STAGE_LABEL: Record<IntentStage, string> = {
  draft: 'draft',
  review: 'review',
  'pr-open': 'PR open',
  merged: 'merged',
  submitted: 'submitted',
  executing: 'executing',
  done: 'done',
};

export function DraftsLedger() {
  const [kind, setKind] = useState<'all' | 'ADR' | 'Plan'>('all');
  const rows = kind === 'all' ? ledger : ledger.filter((d) => d.kind === kind);
  const byStage = (s: IntentStage) => ledger.filter((d) => d.stage === s).length;
  // Drafts that haven't reached a PR are the "invisible until now" set.
  const unsurfaced = ledger.filter((d) => d.stage === 'draft' || d.stage === 'review').length;

  return (
    <PageLayout
      title="drafts ledger"
      breadcrumb={<span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)', letterSpacing: 0.5 }}>PIPELINE OF INTENT</span>}
      freshness={<ConnectionAffordance mode="polling" lastUpdated={new Date().toISOString()} />}
      actions={
        <div style={{ display: 'flex', gap: 4 }}>
          {(['all', 'ADR', 'Plan'] as const).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              style={{
                padding: '5px 12px',
                borderRadius: 2,
                border: `1px solid ${kind === k ? 'var(--tm-border-3)' : 'var(--tm-border)'}`,
                background: kind === k ? 'var(--tm-surface-2)' : 'transparent',
                color: kind === k ? 'var(--tm-t1)' : 'var(--tm-t3)',
                fontFamily: 'var(--tm-mono)',
                fontSize: 11.5,
                cursor: 'pointer',
              }}
            >
              {k}
            </button>
          ))}
        </div>
      }
    >
      {/* Stage rail — a horizontal funnel across the intent pipeline */}
      <div style={{ display: 'flex', gap: 1, marginBottom: 16, border: '1px solid var(--tm-border)', borderRadius: 2, overflow: 'hidden' }}>
        {INTENT_STAGES.map((s) => {
          const n = byStage(s);
          return (
            <div key={s} style={{ flex: 1, padding: '10px 12px', background: n ? 'var(--tm-surface)' : 'var(--tm-bg)', display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span className="tm-tnum" style={{ fontSize: 18, fontWeight: 500, color: n ? `var(--tm-${STAGE_TONE[s]}-fg)` : 'var(--tm-t4)' }}>{n}</span>
              <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.4, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{STAGE_LABEL[s]}</span>
            </div>
          );
        })}
      </div>

      {unsurfaced > 0 && (
        <div style={{ marginBottom: 14, fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t3)' }}>
          <span style={{ color: 'var(--tm-info-fg)' }}>{unsurfaced}</span> doc{unsurfaced === 1 ? '' : 's'} specced but not yet PR'd — the set that used to live only in relay logs.
        </div>
      )}

      <Panel padded={false}>
        {rows.map((d) => (
          <LedgerRow key={d.id} d={d} />
        ))}
      </Panel>
    </PageLayout>
  );
}

function LedgerRow({ d }: { d: LedgerDoc }) {
  const navigate = useNavigate();
  const tone = STAGE_TONE[d.stage];
  const idx = INTENT_STAGES.indexOf(d.stage);
  return (
    <div
      onClick={() => navigate(`/drafts/${d.id}`)}
      style={{ padding: '12px 16px', borderBottom: '1px solid var(--tm-border)', display: 'flex', alignItems: 'center', gap: 14, cursor: 'pointer' }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <span
        title={d.kind}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 30,
          fontFamily: 'var(--tm-mono)',
          fontSize: 9.5,
          letterSpacing: 0.5,
          color: d.kind === 'ADR' ? 'var(--tm-info-fg)' : 'var(--tm-warn-fg)',
          border: `1px solid ${d.kind === 'ADR' ? 'var(--tm-info-edge)' : 'var(--tm-warn-edge)'}`,
          borderRadius: 3,
          padding: '2px 0',
          flexShrink: 0,
        }}
      >
        {d.kind === 'ADR' ? 'ADR' : 'PLN'}
      </span>

      <div style={{ minWidth: 0, flex: '0 1 320px' }}>
        <div style={{ fontSize: 12.5, color: 'var(--tm-t1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.title}</div>
        <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)', display: 'flex', gap: 8, marginTop: 2 }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}><FileText size={10} />{d.owner}</span>
          <span>rev: {d.reviewer}</span>
        </div>
      </div>

      {/* Mini intent-position dots */}
      <div style={{ display: 'flex', gap: 3, flex: 1, justifyContent: 'center' }}>
        {INTENT_STAGES.map((s, i) => (
          <span
            key={s}
            title={STAGE_LABEL[s]}
            style={{
              width: i === idx ? 18 : 6,
              height: 6,
              borderRadius: 999,
              background: i < idx ? 'var(--tm-ok)' : i === idx ? `var(--tm-${tone})` : 'var(--tm-surface-3)',
              transition: 'width 0.2s',
            }}
          />
        ))}
      </div>

      <span
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 10.5,
          color: `var(--tm-${tone}-fg)`,
          background: `var(--tm-${tone}-bg)`,
          border: `1px solid var(--tm-${tone}-edge)`,
          borderRadius: 4,
          padding: '2px 8px',
          flexShrink: 0,
          width: 84,
          textAlign: 'center',
        }}
      >
        {STAGE_LABEL[d.stage]}
      </span>

      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)', width: 64, textAlign: 'right', flexShrink: 0 }}>
        {d.prNumber ? (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, color: 'var(--tm-info-fg)' }}>
            <GitBranch size={10} />#{d.prNumber}
          </span>
        ) : (
          fmt.duration(d.updatedAgeS)
        )}
      </span>
    </div>
  );
}
