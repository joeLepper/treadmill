/**
 * ValidatorGoldViewer — `/review/validator-gold` (ADR-0070 substep 3).
 *
 * Viewer component for the validator-gold review queue. Displays the
 * validator's verdict and LLM recommendation, with label controls.
 * The framework chrome (FlipThroughLayout) owns the page layout, header,
 * and accept/reject buttons.
 */

import { type CSSProperties, type ReactNode, useEffect, useState } from 'react';

import { Button } from '../design/Button';
import { StateBadge } from '../design/StateBadge';
import type {
  ValidatorGoldLabel,
  ValidatorGoldRow,
} from '../api/types';
import type {
  ReviewKindViewerProps,
  ReviewLabelInput,
} from './types';

const VERDICT_OPTIONS: ValidatorGoldLabel[] = [
  'correct-verdict',
  'wrong-verdict',
  'unclear',
];

interface LabelDraft {
  label: ValidatorGoldLabel | null;
  override_reason: string;
  notes: string;
}

const EMPTY_DRAFT: LabelDraft = {
  label: null,
  override_reason: '',
  notes: '',
};

export default function ValidatorGoldViewer({
  row,
  onLabel,
}: ReviewKindViewerProps<ValidatorGoldRow, ValidatorGoldLabel>) {
  const candidate = row.candidate;
  const [draft, setDraft] = useState<LabelDraft>(EMPTY_DRAFT);

  useEffect(() => {
    setDraft(EMPTY_DRAFT);
  }, [row.id]);

  const onSubmit = () => {
    if (!draft.label) {
      alert('Please select a label');
      return;
    }
    const input: ReviewLabelInput = {
      label: draft.label,
      override_reason: draft.override_reason || undefined,
      notes: draft.notes || undefined,
      labeled_by: 'operator',
    };
    onLabel(input);
  };

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '1fr 380px',
        gap: 16,
        alignItems: 'start',
      }}
    >
      <CandidateColumn candidate={candidate} recommendation={candidate.llm_label} />
      <LabelColumn draft={draft} onChange={setDraft} onSubmit={onSubmit} />
    </div>
  );
}

/* ─── Candidate column ───────────────────────────────────────────────── */

function CandidateColumn({
  candidate,
  recommendation,
}: {
  candidate: ValidatorGoldRow;
  recommendation: ValidatorGoldRow['llm_label'];
}) {
  // Find the full recommendation object from candidate
  const rec = {
    label: recommendation,
    confidence: candidate.llm_confidence,
    rationale: candidate.llm_rationale,
    prompt_version: candidate.llm_prompt_version,
    model: candidate.llm_model,
  };

  return (
    <section
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        minWidth: 0,
      }}
    >
      <Header candidate={candidate} />
      <ScriptSection script={candidate.script_excerpt} />
      <ArtifactSection artifact={candidate.artifact_excerpt} />
      <LlmCard rec={rec} />
    </section>
  );
}

function Header({ candidate }: { candidate: ValidatorGoldRow }) {
  return (
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
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Mono style={{ color: 'var(--tm-t3)' }}>
          {candidate.source_step_id.slice(0, 8)}
        </Mono>
        <StateBadge state={candidate.verdict_emitted} size="sm" />
      </div>
    </header>
  );
}

function ScriptSection({ script }: { script: string }) {
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
      <FieldLabel>validation script</FieldLabel>
      <Code>{script}</Code>
    </div>
  );
}

function ArtifactSection({ artifact }: { artifact: string }) {
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
      <FieldLabel>artifact excerpt</FieldLabel>
      <Code>{artifact}</Code>
    </div>
  );
}

function LlmCard({
  rec,
}: {
  rec: {
    label: string;
    confidence: string;
    rationale: string;
    prompt_version: string;
    model: string;
  };
}) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        padding: '12px 14px',
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
      }}
    >
      <FieldLabel>llm recommendation</FieldLabel>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <StateBadge state={rec.label} size="sm" />
        <Mono style={{ color: 'var(--tm-t3)' }}>
          {rec.confidence} confidence
        </Mono>
      </div>
      <Paragraph>{rec.rationale}</Paragraph>
      <div
        style={{
          display: 'flex',
          gap: 8,
          marginTop: 4,
          fontSize: 10.5,
          color: 'var(--tm-t4)',
          fontFamily: 'var(--tm-mono)',
        }}
      >
        <Mono>{rec.prompt_version}</Mono>
        <span>·</span>
        <Mono>{rec.model}</Mono>
      </div>
    </div>
  );
}

/* ─── Label column ─────────────────────────────────────────────────── */

interface LabelColumnProps {
  draft: LabelDraft;
  onChange: (draft: LabelDraft) => void;
  onSubmit: () => void;
}

function LabelColumn({ draft, onChange, onSubmit }: LabelColumnProps) {
  const set = <K extends keyof LabelDraft>(key: K, value: LabelDraft[K]) => {
    onChange({ ...draft, [key]: value });
  };

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
      <Mono style={{ color: 'var(--tm-t3)' }}>// labels</Mono>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <FieldLabel>verdict</FieldLabel>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {VERDICT_OPTIONS.map((opt) => {
            const active = opt === draft.label;
            return (
              <button
                key={opt}
                type="button"
                onClick={() => set('label', opt)}
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
                  textAlign: 'left',
                }}
              >
                {opt}
              </button>
            );
          })}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <FieldLabel>override reason</FieldLabel>
        <textarea
          value={draft.override_reason}
          onChange={(e) => set('override_reason', e.target.value)}
          rows={3}
          placeholder="optional — why you disagree with the LLM"
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 12,
            padding: '8px 10px',
            background: 'var(--tm-bg)',
            border: '1px solid var(--tm-border)',
            borderRadius: 2,
            color: 'var(--tm-t1)',
            resize: 'vertical',
            minHeight: 50,
            outline: 'none',
          }}
        />
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <FieldLabel>notes</FieldLabel>
        <textarea
          value={draft.notes}
          onChange={(e) => set('notes', e.target.value)}
          rows={3}
          placeholder="optional — free-form context for analysis"
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 12,
            padding: '8px 10px',
            background: 'var(--tm-bg)',
            border: '1px solid var(--tm-border)',
            borderRadius: 2,
            color: 'var(--tm-t1)',
            resize: 'vertical',
            minHeight: 50,
            outline: 'none',
          }}
        />
      </div>

      <Button kind="primary" onClick={onSubmit}>
        submit & next
      </Button>
    </aside>
  );
}

/* ─── Primitives ───────────────────────────────────────────────────── */

function FieldLabel({ children }: { children: ReactNode }) {
  return (
    <span
      style={{
        fontFamily: 'var(--tm-mono)',
        fontSize: 10,
        letterSpacing: 1.2,
        textTransform: 'uppercase',
        color: 'var(--tm-t4)',
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

function Code({ children }: { children: ReactNode }) {
  return (
    <pre
      style={{
        margin: 0,
        fontFamily: 'var(--tm-mono)',
        fontSize: 11.5,
        color: 'var(--tm-t1)',
        background: 'var(--tm-bg)',
        padding: '8px 10px',
        borderRadius: 2,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      {children}
    </pre>
  );
}

function Paragraph({ children }: { children: ReactNode }) {
  return (
    <p
      style={{
        margin: 0,
        fontFamily: 'var(--tm-mono)',
        fontSize: 12.5,
        color: 'var(--tm-t1)',
        lineHeight: 1.55,
        whiteSpace: 'pre-wrap',
      }}
    >
      {children}
    </p>
  );
}
