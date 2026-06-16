/**
 * Doc Detail — read an ADR/plan from the drafts ledger.
 *
 * Renders the actual written doc: prose as styled markdown, and (for plans)
 * the sequence_of_work as visual task cards via <DocBody>. Reached from a
 * ledger row; the meta header carries the intent-pipeline position + owner
 * / reviewer / PR so the reader keeps the ledger context while reading.
 */

import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, ExternalLink, GitBranch } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { DocBody } from '../design/DocBody';
import type { Tone } from '../design/fmt';
import { DOC_CONTENT, realLedger } from '../api/docContent';
import { INTENT_STAGES, type IntentStage } from '../api/v2mock';

const STAGE_TONE: Record<IntentStage, Tone> = {
  draft: 'muted', review: 'info', 'pr-open': 'warn', merged: 'ok', submitted: 'info', executing: 'warn', done: 'ok',
};
const STAGE_LABEL: Record<IntentStage, string> = {
  draft: 'draft', review: 'review', 'pr-open': 'PR open', merged: 'merged', submitted: 'submitted', executing: 'executing', done: 'done',
};

export function DocDetail() {
  const { docId } = useParams();
  const navigate = useNavigate();
  const doc = realLedger.find((d) => d.id === docId);
  const content = docId ? DOC_CONTENT[docId] : undefined;

  if (!doc) {
    return (
      <PageLayout title="not found" breadcrumb={<Crumb onBack={() => navigate('/adrs')} />}>
        <Panel padded><span style={{ color: 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 12 }}>// no ledger entry for {docId}</span></Panel>
      </PageLayout>
    );
  }

  const tone = STAGE_TONE[doc.stage];
  const idx = INTENT_STAGES.indexOf(doc.stage);

  return (
    <PageLayout
      title={doc.title}
      breadcrumb={<Crumb onBack={() => navigate('/adrs')} />}
      actions={
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          {doc.prNumber && (
            <a
              href={`https://github.com/${doc.repo}/pull/${doc.prNumber}`}
              target="_blank"
              rel="noreferrer"
              style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontFamily: 'var(--tm-mono)', fontSize: 11.5, color: 'var(--tm-info-fg)', textDecoration: 'none', border: '1px solid var(--tm-info-edge)', borderRadius: 2, padding: '5px 10px' }}
            >
              <GitBranch size={12} /> #{doc.prNumber} <ExternalLink size={11} />
            </a>
          )}
          <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11.5, color: `var(--tm-${tone}-fg)`, background: `var(--tm-${tone}-bg)`, border: `1px solid var(--tm-${tone}-edge)`, borderRadius: 4, padding: '4px 11px' }}>
            {STAGE_LABEL[doc.stage]}
          </span>
        </div>
      }
    >
      {/* Meta header — keeps ledger context while reading */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginBottom: 16, padding: '12px 16px', border: '1px solid var(--tm-border)', borderRadius: 3, background: 'var(--tm-surface)', flexWrap: 'wrap' }}>
        <Meta label="kind" value={doc.kind} />
        <Meta label="repo" value={doc.repo} mono />
        <Meta label="owner" value={doc.owner} />
        <Meta label="reviewer" value={doc.reviewer} />
        {/* intent-pipeline position dots */}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
          {INTENT_STAGES.map((s, i) => (
            <span key={s} title={STAGE_LABEL[s]} style={{ width: i === idx ? 16 : 6, height: 6, borderRadius: 999, background: i < idx ? 'var(--tm-ok)' : i === idx ? `var(--tm-${tone})` : 'var(--tm-surface-3)', transition: 'width 0.2s' }} />
          ))}
        </div>
      </div>

      {content ? (
        <Panel padded style={{ background: 'var(--tm-bg)' }}>
          <div style={{ maxWidth: 880 }}>
            <DocBody source={content} />
          </div>
        </Panel>
      ) : (
        <Panel padded>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, color: 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 12 }}>
            <span style={{ color: 'var(--tm-t2)' }}>// {doc.stage === 'draft' ? 'drafting — not yet committed to a file' : 'content not in the bundled snapshot'}</span>
            <span style={{ color: 'var(--tm-t4)', fontSize: 11 }}>
              {doc.stage === 'draft'
                ? 'This doc exists as intent on the ledger; its body lands when the author commits it. The ledger surfaces it now so the work-in-intent is visible — the gap v2 closes.'
                : 'Live doc-API read-through (repo / context store) is the follow-up to the bundled snapshot.'}
            </span>
          </div>
        </Panel>
      )}
    </PageLayout>
  );
}

function Crumb({ onBack }: { onBack: () => void }) {
  return (
    <button onClick={onBack} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, background: 'transparent', border: 'none', color: 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 10.5, letterSpacing: 0.5, cursor: 'pointer', padding: 0, textTransform: 'uppercase' }}>
      <ArrowLeft size={12} /> ADR ledger
    </button>
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
