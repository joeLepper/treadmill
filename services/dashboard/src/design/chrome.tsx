/**
 * Small chrome primitives: <RepoCell>, <AccountPill>, <WorkflowChip>,
 * <PipelinePill>, <MetricChip>. Co-located because they're all
 * single-purpose cells used inside tables and headers.
 */

import type { ReactNode } from 'react';
import { ExternalLink } from 'lucide-react';
import type { Tone } from './fmt';
import { toneOf } from './StateBadge';

/* ─── <RepoCell> — repo name + mode pill ─────────────────────────────
 *
 * Adapt-mode repos get a visually distinct treatment (subtle inset border +
 * an external-link glyph) so the operator can scan for them at a glance.
 * Per DESIGN.md §"New surfaces": mode-aware repo badge.
 */
export type RepoMode = 'conform' | 'adapt';

export function RepoCell({ repo, mode }: { repo: string; mode: RepoMode | null | undefined }) {
  const isAdapt = mode === 'adapt';
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 7,
        fontFamily: 'var(--tm-mono)',
        fontSize: 12,
        color: 'var(--tm-t2)',
        padding: isAdapt ? '1px 6px 1px 4px' : 0,
        border: isAdapt ? '1px solid var(--tm-border-2)' : 'none',
        borderRadius: 3,
        background: isAdapt ? 'var(--tm-surface-2)' : 'transparent',
      }}
    >
      <span style={{ color: 'var(--tm-t1)' }}>{repo}</span>
      {mode && (
        <span
          title={
            mode === 'conform'
              ? 'conform mode — Treadmill commits scaffolding'
              : 'adapt mode — repo stays pristine, docs external'
          }
          style={{
            fontSize: 9,
            padding: '1px 5px',
            borderRadius: 2,
            background: mode === 'conform' ? 'var(--tm-info-bg)' : 'var(--tm-warn-bg)',
            color: mode === 'conform' ? 'var(--tm-info-fg)' : 'var(--tm-warn-fg)',
            fontWeight: 500,
            letterSpacing: 0.4,
            textTransform: 'uppercase',
          }}
        >
          {mode}
        </span>
      )}
      {isAdapt && <ExternalLink size={10} />}
    </span>
  );
}

/* ─── <AccountPill> — Claude account this repo bills (ADR-0055) ───────
 *
 * Neutral chrome — no per-account color (the operator distinguishes by
 * name, not hue). Per DESIGN.md "No decorative color."
 */
export function AccountPill({ name }: { name: string }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        padding: '1px 7px',
        borderRadius: 999,
        fontFamily: 'var(--tm-mono)',
        fontSize: 10.5,
        background: 'var(--tm-surface-3)',
        color: 'var(--tm-t2)',
        border: '1px solid var(--tm-border-2)',
      }}
    >
      <span style={{ width: 5, height: 5, borderRadius: 999, background: 'var(--tm-t3)' }} />
      {name}
    </span>
  );
}

/* ─── <WorkflowChip> — workflow ID + status dot ───────────────────────
 *
 * Used in the workflow-runs timeline on Task Detail. Status tone comes
 * from `toneOf(status)` so we never hand-roll the mapping.
 */
export function WorkflowChip({
  id,
  status,
}: {
  id: string;
  status: string | null | undefined;
}) {
  const tone = toneOf(status);
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        padding: '1px 7px',
        borderRadius: 2,
        fontFamily: 'var(--tm-mono)',
        fontSize: 10.5,
        background: 'var(--tm-surface-2)',
        color: 'var(--tm-t1)',
        border: '1px solid var(--tm-border-2)',
      }}
    >
      <span
        style={{ width: 4, height: 4, borderRadius: 999, background: `var(--tm-${tone})` }}
      />
      {id}
    </span>
  );
}

/* ─── <PipelinePill> — planning → coding → review[●] ──────────────────
 *
 * Renders the role progression as a compact pill, so the overview table
 * can answer "which roles are running now" without the operator clicking
 * into the task. Per DESIGN.md rule C ("render the pipeline, not the row").
 */
export interface PipelineStep {
  role: string;
  status: 'done' | 'running' | 'pending' | 'failed' | string;
}

export function PipelinePill({ steps, dense }: { steps: PipelineStep[]; dense?: boolean }) {
  const sz = dense ? 9 : 10;
  const dot = (s: string) => {
    if (s === 'done') return 'var(--tm-ok)';
    if (s === 'running') return 'var(--tm-warn)';
    if (s === 'failed') return 'var(--tm-danger)';
    return 'var(--tm-t4)';
  };
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontFamily: 'var(--tm-mono)',
        fontSize: sz + 1.5,
        color: 'var(--tm-t2)',
        padding: dense ? '1px 6px' : '2px 7px',
        borderRadius: 3,
        background: 'var(--tm-surface-2)',
        border: '1px solid var(--tm-border)',
        whiteSpace: 'nowrap',
      }}
    >
      {steps.map((s, i) => (
        <span
          key={i}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}
        >
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 3,
              color: s.status === 'pending' ? 'var(--tm-t4)' : 'var(--tm-t1)',
              textDecoration:
                s.status === 'failed' ? 'underline wavy var(--tm-danger)' : 'none',
              textUnderlineOffset: 3,
            }}
          >
            <span
              style={{
                width: sz - 4,
                height: sz - 4,
                borderRadius: 999,
                background: dot(s.status),
                boxShadow: s.status === 'running' ? '0 0 0 2px var(--tm-warn-bg)' : 'none',
                animation:
                  s.status === 'running' ? 'tm-pulse-soft 1.6s ease-in-out infinite' : 'none',
                flexShrink: 0,
              }}
            />
            {s.role}
          </span>
          {i < steps.length - 1 && <span style={{ color: 'var(--tm-t4)' }}>→</span>}
        </span>
      ))}
    </span>
  );
}

/* ─── <MetricChip> — labeled metric value, for header strips ──────────
 *
 * Used in the per-account spend strip and the fleet heartbeat row on
 * Overview. Tone (left-edge color) flags freshness for heartbeats.
 */
export function MetricChip({
  label,
  value,
  sub,
  tone,
  icon,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  tone?: Tone;
  icon?: ReactNode;
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '6px 12px',
        background: 'var(--tm-surface-2)',
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        borderLeft: tone ? `2px solid var(--tm-${tone})` : '1px solid var(--tm-border)',
        minHeight: 32,
      }}
    >
      {icon && (
        <span
          style={{
            color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t3)',
            display: 'flex',
          }}
        >
          {icon}
        </span>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 9.5,
            letterSpacing: 0.8,
            color: 'var(--tm-t4)',
            textTransform: 'uppercase',
          }}
        >
          {label}
        </span>
        <span
          className="tm-tnum"
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 13,
            fontWeight: 500,
            color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)',
          }}
        >
          {value}
          {sub && (
            <span style={{ color: 'var(--tm-t4)', marginLeft: 4, fontWeight: 400 }}>{sub}</span>
          )}
        </span>
      </div>
    </div>
  );
}
