/**
 * DspyVariantPrReview — `/review/dspy-variant-pr` (ADR-0070 substep 4.3).
 *
 * Flip-through labeling UI for operator review of DSPy variant PR candidates.
 * One unlabeled row at a time: left column carries evidence (judge role,
 * PR link, scores, patch diff, corpus URI); right column carries the LLM
 * recommendation card plus the label form with override_reason guard.
 *
 * Default export so a future import.meta.glob registry can auto-discover it.
 */

import { type CSSProperties, type ReactNode, useEffect, useState } from 'react';

import { Button } from '../design/Button';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { PageLayout } from '../design/PageLayout';
import { StateBadge } from '../design/StateBadge';

import {
  useDspyVariantPrQueue,
  useDspyVariantPrStats,
  useLabelDspyVariantPr,
} from '../api/queries';
import { useLiveSim } from '../api/sim';
import type {
  DspyVariantPrLabel,
  DspyVariantPrLabelInput,
  DspyVariantPrRow,
} from '../api/types';

const DEFAULT_LABELED_BY = 'operator';

const VERDICT_OPTIONS: DspyVariantPrLabel[] = ['merge', 'revise', 'drop'];

interface Draft {
  label_verdict: DspyVariantPrLabel | null;
  label_notes: string;
  override_reason: string;
}

const EMPTY_DRAFT: Draft = {
  label_verdict: null,
  label_notes: '',
  override_reason: '',
};

export default function DspyVariantPrReview() {
  const sim = useLiveSim();
  const { data: rows = [], isLoading, error, refetch } = useDspyVariantPrQueue();
  const { data: stats } = useDspyVariantPrStats();
  const labelMutation = useLabelDspyVariantPr();

  const current = rows[0] ?? null;

  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  useEffect(() => {
    setDraft(EMPTY_DRAFT);
  }, [current?.id]);

  const disagreesWithLlm =
    draft.label_verdict !== null &&
    current !== null &&
    draft.label_verdict !== current.llm_label;

  const overrideRequired = disagreesWithLlm && !draft.override_reason.trim();

  const onSubmit = () => {
    if (!current || !draft.label_verdict) return;
    const body: DspyVariantPrLabelInput = {
      label_verdict: draft.label_verdict,
      label_notes: draft.label_notes.trim() || null,
      label_override_reason: disagreesWithLlm
        ? draft.override_reason.trim() || null
        : null,
      labeled_by: DEFAULT_LABELED_BY,
    };
    labelMutation.mutate({ id: current.id, label: body });
  };

  return (
    <PageLayout
      title="review · dspy-variant-pr"
      loading={isLoading}
      error={error as Error | null}
      breadcrumb={
        <span
          style={{
            fontSize: 10.5,
            color: 'var(--tm-t4)',
            fontFamily: 'var(--tm-mono)',
            letterSpacing: 0.8,
            textTransform: 'uppercase',
          }}
        >
          treadmill · operator
        </span>
      }
      freshness={<ConnectionAffordance mode={sim.mode} lastUpdated={sim.lastUpdated} />}
      actions={
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: 'var(--tm-t3)',
          }}
        >
          <span style={{ color: 'var(--tm-t4)' }}>queue</span>{' '}
          <span className="tm-tnum" style={{ color: 'var(--tm-t1)' }}>
            {stats?.unlabeled ?? rows.length}
          </span>{' '}
          <span style={{ color: 'var(--tm-t4)' }}>unlabeled</span>
        </span>
      }
    >
      {!current ? (
        <EmptyQueue onRefresh={() => refetch()} />
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr 380px',
            gap: 16,
            alignItems: 'start',
          }}
        >
          <EvidenceColumn row={current} />
          <LabelColumn
            row={current}
            draft={draft}
            onChange={setDraft}
            onSubmit={onSubmit}
            submitting={labelMutation.isPending}
            error={labelMutation.error as Error | null}
            overrideRequired={overrideRequired}
          />
        </div>
      )}
    </PageLayout>
  );
}

/* ─── Empty state ──────────────────────────────────────────────────── */

function EmptyQueue({ onRefresh }: { onRefresh: () => void }) {
  return (
    <div
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
        padding: '32px 16px',
        textAlign: 'center',
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        alignItems: 'center',
        fontFamily: 'var(--tm-mono)',
        color: 'var(--tm-t3)',
        fontSize: 12,
      }}
    >
      <div style={{ fontSize: 13, color: 'var(--tm-t1)' }}>// queue empty</div>
      <div>No unlabeled DSPy variant PR candidates. Nothing to do here.</div>
      <Button size="sm" onClick={onRefresh}>
        check again
      </Button>
    </div>
  );
}

/* ─── Evidence column ──────────────────────────────────────────────── */

function EvidenceColumn({ row }: { row: DspyVariantPrRow }) {
  const sign = row.improvement >= 0 ? '+' : '';
  const improvementStr = `${sign}${row.improvement.toFixed(3)}`;

  return (
    <section
      style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}
    >
      <header
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
          padding: '12px 14px',
          border: '1px solid var(--tm-border)',
          borderRadius: 2,
          background: 'var(--tm-surface)',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            flexWrap: 'wrap',
          }}
        >
          <Mono style={{ color: 'var(--tm-t1)', fontWeight: 500 }}>
            {row.judge_role}
          </Mono>
          <a
            href={row.source_pr_url}
            target="_blank"
            rel="noreferrer"
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 11.5,
              color: 'var(--tm-accent)',
              textDecoration: 'none',
            }}
          >
            PR #{row.source_pr_number}
          </a>
        </div>
        <Mono style={{ color: 'var(--tm-t4)', fontSize: 10.5 }}>
          {row.created_at}
        </Mono>
      </header>

      <FieldBlock label="scores">
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <span
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 11.5,
              color: 'var(--tm-t3)',
            }}
          >
            current
          </span>
          <StateBadge state={`${row.current_score.toFixed(3)}`} size="sm" />
          <span
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 11.5,
              color: 'var(--tm-t3)',
            }}
          >
            variant
          </span>
          <StateBadge state={`${row.variant_score.toFixed(3)}`} size="sm" />
          <span
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 12,
              color: row.improvement >= 0 ? 'var(--tm-success-fg, #4caf50)' : 'var(--tm-danger-fg)',
              fontWeight: 600,
            }}
          >
            {improvementStr}
          </span>
        </div>
      </FieldBlock>

      <FieldBlock label="patch diff">
        <pre
          style={{
            margin: 0,
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: 'var(--tm-t2)',
            background: 'var(--tm-bg)',
            border: '1px solid var(--tm-border)',
            borderRadius: 2,
            padding: '8px 10px',
            overflowX: 'auto',
            whiteSpace: 'pre',
            lineHeight: 1.5,
          }}
        >
          {row.patch_diff}
        </pre>
      </FieldBlock>

      <FieldBlock label="corpus">
        <Mono style={{ wordBreak: 'break-all' }}>{row.corpus_s3_uri}</Mono>
      </FieldBlock>
    </section>
  );
}

/* ─── Label column ─────────────────────────────────────────────────── */

interface LabelColumnProps {
  row: DspyVariantPrRow;
  draft: Draft;
  onChange: (draft: Draft) => void;
  onSubmit: () => void;
  submitting: boolean;
  error: Error | null;
  overrideRequired: boolean;
}

function LabelColumn({
  row,
  draft,
  onChange,
  onSubmit,
  submitting,
  error,
  overrideRequired,
}: LabelColumnProps) {
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    onChange({ ...draft, [key]: value });
  };

  const submitDisabled =
    submitting || draft.label_verdict === null || overrideRequired;

  return (
    <aside
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        padding: 14,
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
        position: 'sticky',
        top: 16,
      }}
    >
      <Mono style={{ color: 'var(--tm-t3)' }}>// llm recommendation</Mono>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
          padding: '10px 12px',
          border: '1px solid var(--tm-border)',
          borderRadius: 2,
          background: 'var(--tm-bg)',
        }}
      >
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <StateBadge state={row.llm_label} size="sm" />
          <StateBadge state={row.llm_confidence} size="sm" />
        </div>
        <p
          style={{
            margin: 0,
            fontFamily: 'var(--tm-mono)',
            fontSize: 12,
            color: 'var(--tm-t2)',
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
          }}
        >
          {row.llm_rationale}
        </p>
        <Mono style={{ color: 'var(--tm-t4)', fontSize: 10.5 }}>
          {row.llm_prompt_version} · {row.llm_model}
        </Mono>
      </div>

      <Mono style={{ color: 'var(--tm-t3)' }}>// verdict</Mono>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <FieldLabel>label verdict</FieldLabel>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {VERDICT_OPTIONS.map((opt) => {
            const active = opt === draft.label_verdict;
            return (
              <button
                key={opt}
                type="button"
                onClick={() => set('label_verdict', active ? null : opt)}
                style={{
                  padding: '5px 10px',
                  fontFamily: 'var(--tm-mono)',
                  fontSize: 11.5,
                  background: active ? 'var(--tm-t1)' : 'transparent',
                  color: active ? 'var(--tm-bg)' : 'var(--tm-t2)',
                  border: '1px solid var(--tm-border-2)',
                  borderRadius: 2,
                  cursor: 'pointer',
                  textTransform: 'lowercase',
                  letterSpacing: 0.3,
                  transition: 'background 0.12s',
                }}
              >
                {opt}
              </button>
            );
          })}
          <button
            type="button"
            onClick={() => set('label_verdict', null)}
            style={{
              padding: '5px 10px',
              fontFamily: 'var(--tm-mono)',
              fontSize: 11.5,
              background: draft.label_verdict === null ? 'var(--tm-t1)' : 'transparent',
              color: draft.label_verdict === null ? 'var(--tm-bg)' : 'var(--tm-t2)',
              border: '1px solid var(--tm-border-2)',
              borderRadius: 2,
              cursor: 'pointer',
              textTransform: 'lowercase',
              letterSpacing: 0.3,
              transition: 'background 0.12s',
            }}
          >
            skip
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <FieldLabel>notes</FieldLabel>
        <textarea
          value={draft.label_notes}
          onChange={(e) => set('label_notes', e.target.value)}
          rows={3}
          placeholder="optional — free-form context"
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 12,
            padding: '8px 10px',
            background: 'var(--tm-bg)',
            border: '1px solid var(--tm-border)',
            borderRadius: 2,
            color: 'var(--tm-t1)',
            resize: 'vertical',
            minHeight: 56,
            outline: 'none',
          }}
        />
      </div>

      {draft.label_verdict !== null && draft.label_verdict !== row.llm_label && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <FieldLabel style={{ color: overrideRequired ? 'var(--tm-danger-fg)' : undefined }}>
            override reason {overrideRequired && '(required)'}
          </FieldLabel>
          <textarea
            value={draft.override_reason}
            onChange={(e) => set('override_reason', e.target.value)}
            rows={3}
            placeholder="why does your verdict differ from the LLM's?"
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 12,
              padding: '8px 10px',
              background: 'var(--tm-bg)',
              border: `1px solid ${overrideRequired ? 'var(--tm-danger-edge)' : 'var(--tm-border)'}`,
              borderRadius: 2,
              color: 'var(--tm-t1)',
              resize: 'vertical',
              minHeight: 56,
              outline: 'none',
            }}
          />
        </div>
      )}

      {error && (
        <div
          style={{
            border: '1px solid var(--tm-danger-edge)',
            background: 'var(--tm-danger-bg)',
            color: 'var(--tm-danger-fg)',
            padding: '6px 10px',
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            borderRadius: 2,
          }}
        >
          {error.message}
        </div>
      )}

      <Button kind="primary" onClick={onSubmit} disabled={submitDisabled}>
        {submitting ? 'submitting…' : 'submit & next'}
      </Button>
    </aside>
  );
}

/* ─── Primitives (kept local) ───────────────────────────────────────── */

function FieldBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        padding: '12px 14px',
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
      }}
    >
      <FieldLabel>{label}</FieldLabel>
      {children}
    </div>
  );
}

function FieldLabel({
  children,
  style,
}: {
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <span
      style={{
        fontFamily: 'var(--tm-mono)',
        fontSize: 10,
        letterSpacing: 1.2,
        textTransform: 'uppercase',
        color: 'var(--tm-t4)',
        ...style,
      }}
    >
      {children}
    </span>
  );
}

function Mono({
  children,
  style,
}: {
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <span
      style={{
        fontFamily: 'var(--tm-mono)',
        fontSize: 11.5,
        color: 'var(--tm-t2)',
        ...style,
      }}
    >
      {children}
    </span>
  );
}
