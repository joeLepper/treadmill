/**
 * TriageLabeling — `/triage` (ADR-0061).
 *
 * Flip-through labeling UI: shows ONE unlabeled triage finding at a
 * time. Left column carries the evidence the labeler scores against
 * (screenshot, observation, evidence_pointer, proposed_resolution).
 * Right column carries the four ADR-0061 label questions
 * (``is_real_bug`` / ``severity`` / ``category`` / ``fix_in_dsl`` + free
 * notes) plus a Submit button.
 *
 * Per the labeling-UI-workflow pin and ADR-0061 v1: "Skip" leaves the
 * field ``null`` — null is itself a signal the optimizer trains against.
 * Submitting writes the labels via ``useLabelFinding`` and advances to
 * the next finding via the unlabeled-cache optimistic update.
 */

import { type CSSProperties, type ReactNode, useEffect, useState } from 'react';

import { Button } from '../design/Button';
import { PageLayout } from '../design/PageLayout';
import { StateBadge } from '../design/StateBadge';

import { useLabelFinding, useUnlabeledFindings } from '../api/queries';
import type {
  TriageCategory,
  TriageFinding,
  TriageLabelInput,
  TriageSeverity,
} from '../api/types';

const CATEGORY_OPTIONS: TriageCategory[] = [
  'console_error',
  'network_failure',
  'broken_asset',
  'accessibility',
  'layout_overflow',
  'consistency',
  'dead_affordance',
  'loading_state',
  'other',
];

const SEVERITY_OPTIONS: TriageSeverity[] = ['high', 'medium', 'low'];

const DEFAULT_LABELED_BY = 'operator';

interface LabelDraft {
  label_is_real_bug: boolean | null;
  label_severity: TriageSeverity | null;
  label_category: TriageCategory | null;
  label_fix_in_dsl: boolean | null;
  label_notes: string;
}

const EMPTY_DRAFT: LabelDraft = {
  label_is_real_bug: null,
  label_severity: null,
  label_category: null,
  label_fix_in_dsl: null,
  label_notes: '',
};

export function TriageLabeling() {
  const { data: findings = [], isLoading, error, refetch } = useUnlabeledFindings();
  const label = useLabelFinding();

  const current = findings[0] ?? null;
  const remaining = findings.length;

  // Draft state is reset whenever the current finding changes — the
  // operator's prior selections shouldn't leak into the next row.
  const [draft, setDraft] = useState<LabelDraft>(EMPTY_DRAFT);
  useEffect(() => {
    setDraft(EMPTY_DRAFT);
  }, [current?.finding_id]);

  const onSubmit = () => {
    if (!current) return;
    const body: TriageLabelInput = {
      label_is_real_bug: draft.label_is_real_bug,
      label_severity: draft.label_severity,
      label_category: draft.label_category,
      label_fix_in_dsl: draft.label_fix_in_dsl,
      label_notes: draft.label_notes ? draft.label_notes : null,
      labeled_by: DEFAULT_LABELED_BY,
    };
    label.mutate({ findingId: current.finding_id, label: body });
  };

  return (
    <PageLayout
      title="triage · labeling"
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
            {remaining}
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
          <EvidenceColumn finding={current} />
          <LabelColumn
            draft={draft}
            onChange={setDraft}
            onSubmit={onSubmit}
            submitting={label.isPending}
            error={label.error as Error | null}
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
      <div style={{ fontSize: 13, color: 'var(--tm-t1)' }}>
        // queue empty
      </div>
      <div>No unlabeled triage findings. Nothing to do here.</div>
      <Button size="sm" onClick={onRefresh}>
        check again
      </Button>
    </div>
  );
}

/* ─── Evidence column ──────────────────────────────────────────────── */

function EvidenceColumn({ finding }: { finding: TriageFinding }) {
  return (
    <section
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        minWidth: 0,
      }}
    >
      <Header finding={finding} />
      <Screenshot uri={finding.screenshot_uri} alt={finding.observation} />
      <FieldBlock label="observation">
        <Paragraph>{finding.observation}</Paragraph>
      </FieldBlock>
      <FieldBlock label="evidence pointer">
        <Mono>{finding.evidence_pointer}</Mono>
      </FieldBlock>
      <FieldBlock label="proposed resolution">
        <Paragraph>{finding.proposed_resolution}</Paragraph>
      </FieldBlock>
    </section>
  );
}

function Header({ finding }: { finding: TriageFinding }) {
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
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          flexWrap: 'wrap',
        }}
      >
        <Mono style={{ color: 'var(--tm-t3)' }}>
          {finding.finding_id.slice(0, 8)}
        </Mono>
        <StateBadge state={finding.severity} size="sm" />
        <StateBadge state={finding.dispatch_action} size="sm" />
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: 'var(--tm-t4)',
            letterSpacing: 0.4,
          }}
        >
          {finding.category} · {finding.confidence} confidence
        </span>
      </div>
      <div
        style={{
          display: 'flex',
          gap: 12,
          alignItems: 'center',
          fontFamily: 'var(--tm-mono)',
          fontSize: 10.5,
          color: 'var(--tm-t4)',
          flexWrap: 'wrap',
        }}
      >
        <span>{finding.target_url}</span>
        <span>·</span>
        <span>
          {finding.viewport_w}×{finding.viewport_h}
        </span>
        <span>·</span>
        <span>{finding.git_sha}</span>
        <span>·</span>
        <span>{finding.prompt_version}</span>
      </div>
    </header>
  );
}

function Screenshot({ uri, alt }: { uri: string; alt: string }) {
  // S3 URIs (``s3://…``) can't be rendered directly. The triage
  // pipeline currently writes ``s3://…/screen.png``; the dashboard
  // doesn't yet have a presign endpoint, so http(s) URIs render
  // directly and S3 URIs fall back to a labeled link until the
  // presign surface lands.
  const isHttp = uri.startsWith('http://') || uri.startsWith('https://');
  if (!isHttp) {
    return (
      <FieldBlock label="screenshot">
        <Mono style={{ wordBreak: 'break-all' }}>{uri}</Mono>
        <div
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 10.5,
            color: 'var(--tm-t4)',
            marginTop: 4,
          }}
        >
          (S3 URI — fetch via{' '}
          <code style={{ color: 'var(--tm-t3)' }}>aws s3 cp</code> to
          inspect locally)
        </div>
      </FieldBlock>
    );
  }
  return (
    <FieldBlock label="screenshot">
      <img
        src={uri}
        alt={alt}
        loading="lazy"
        style={{
          maxWidth: '100%',
          height: 'auto',
          border: '1px solid var(--tm-border)',
          borderRadius: 2,
          display: 'block',
        }}
      />
    </FieldBlock>
  );
}

/* ─── Label column ─────────────────────────────────────────────────── */

interface LabelColumnProps {
  draft: LabelDraft;
  onChange: (draft: LabelDraft) => void;
  onSubmit: () => void;
  submitting: boolean;
  error: Error | null;
}

function LabelColumn({
  draft,
  onChange,
  onSubmit,
  submitting,
  error,
}: LabelColumnProps) {
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

      <ChoiceRow
        label="is real bug?"
        value={tristate(draft.label_is_real_bug)}
        onChange={(v) => set('label_is_real_bug', fromTristate(v))}
        options={['Yes', 'No', 'Skip']}
      />

      <ChoiceRow
        label="severity"
        value={draft.label_severity ?? 'Skip'}
        onChange={(v) =>
          set(
            'label_severity',
            v === 'Skip' ? null : (v as TriageSeverity),
          )
        }
        options={[...SEVERITY_OPTIONS, 'Skip']}
      />

      <SelectRow
        label="category"
        value={draft.label_category}
        onChange={(v) => set('label_category', v)}
        options={CATEGORY_OPTIONS}
      />

      <ChoiceRow
        label="fix in dsl?"
        value={tristate(draft.label_fix_in_dsl)}
        onChange={(v) => set('label_fix_in_dsl', fromTristate(v))}
        options={['Yes', 'No', 'Skip']}
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <FieldLabel>notes</FieldLabel>
        <textarea
          value={draft.label_notes}
          onChange={(e) => set('label_notes', e.target.value)}
          rows={4}
          placeholder="optional — free-form context for the labeler"
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 12,
            padding: '8px 10px',
            background: 'var(--tm-bg)',
            border: '1px solid var(--tm-border)',
            borderRadius: 2,
            color: 'var(--tm-t1)',
            resize: 'vertical',
            minHeight: 70,
            outline: 'none',
          }}
        />
      </div>

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

      <Button kind="primary" onClick={onSubmit} disabled={submitting}>
        {submitting ? 'submitting…' : 'submit & next'}
      </Button>

      <div
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 10,
          color: 'var(--tm-t4)',
          letterSpacing: 0.4,
        }}
      >
        skip = leave null. null is a signal per ADR-0061.
      </div>
    </aside>
  );
}

/* ─── Primitives (kept local — these are not page-chrome) ───────────── */

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

function ChoiceRow({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <FieldLabel>{label}</FieldLabel>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {options.map((opt) => {
          const active = opt === value;
          return (
            <button
              key={opt}
              type="button"
              onClick={() => onChange(opt)}
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
      </div>
    </div>
  );
}

function SelectRow({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: TriageCategory | null;
  onChange: (v: TriageCategory | null) => void;
  options: TriageCategory[];
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <FieldLabel>{label}</FieldLabel>
      <div style={{ display: 'flex', gap: 6 }}>
        <select
          value={value ?? ''}
          onChange={(e) =>
            onChange(e.target.value ? (e.target.value as TriageCategory) : null)
          }
          style={{
            flex: 1,
            padding: '5px 8px',
            fontFamily: 'var(--tm-mono)',
            fontSize: 11.5,
            background: 'var(--tm-bg)',
            color: 'var(--tm-t1)',
            border: '1px solid var(--tm-border)',
            borderRadius: 2,
            outline: 'none',
          }}
        >
          <option value="">— skip —</option>
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => onChange(null)}
          style={{
            padding: '5px 10px',
            fontFamily: 'var(--tm-mono)',
            fontSize: 11.5,
            background: value === null ? 'var(--tm-t1)' : 'transparent',
            color: value === null ? 'var(--tm-bg)' : 'var(--tm-t2)',
            border: '1px solid var(--tm-border-2)',
            borderRadius: 2,
            cursor: 'pointer',
            textTransform: 'lowercase',
          }}
        >
          Skip
        </button>
      </div>
    </div>
  );
}

/* ─── Tristate helpers (Yes/No/Skip ↔ boolean | null) ──────────────── */

function tristate(value: boolean | null): 'Yes' | 'No' | 'Skip' {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  return 'Skip';
}

function fromTristate(value: string): boolean | null {
  if (value === 'Yes') return true;
  if (value === 'No') return false;
  return null;
}

