/**
 * <StateBadge> — the ONE badge for the whole UI.
 *
 * Closed enumerated state vocabulary; action-only color. Per DESIGN.md
 * mandatory rule #1: "Exactly one StateBadge component. No per-entity
 * variants. The entity type is communicated by an icon prefix or container
 * chrome, not by a separate Badge variant."
 *
 * Tone palette is closed: danger / warn / ok / muted (+ info for chrome).
 * Cancelled / superseded / archived are muted gray, never red — only red
 * for "needs attention now" (rule #6).
 */

import type { CSSProperties, ReactNode } from 'react';
import type { Tone } from './fmt';

/** Concrete states map to one of four operator-action tones. */
const STATE_TONE: Record<string, Tone> = {
  // ok — good outcome
  done: 'ok',
  merged: 'ok',
  validated: 'ok',
  mergeable: 'ok',
  passed: 'ok',
  success: 'ok',
  completed: 'ok',
  approved: 'ok',
  clean: 'ok',

  // warn — in flight, watch
  executing: 'warn',
  running: 'warn',
  pending: 'warn',
  awaiting_review: 'warn',
  queued: 'warn',
  registered: 'warn',
  checking: 'warn',
  'needs-more-info': 'warn',

  // danger — needs attention now
  failed: 'danger',
  'blocked-on-conflict': 'danger',
  'blocked-on-ci': 'danger',
  'blocked-on-review': 'danger',
  'blocked-on-validate': 'danger',
  changes_requested: 'danger',
  conflicting: 'danger',
  error: 'danger',
  blocked: 'danger',
  escalated: 'danger',

  // muted — explicit stop / archived
  cancelled: 'muted',
  superseded: 'muted',
  archived: 'muted',
  abandoned: 'muted',
  closed: 'muted',
};

/**
 * Map a state value to its operator-action tone. Honors `wf-*` dynamic
 * stages (e.g. "wf-feedback: executing", "wf-ci-fix: failed") by parsing
 * the trailing word.
 */
export function toneOf(state: string | null | undefined): Tone {
  if (!state) return 'muted';
  if (STATE_TONE[state]) return STATE_TONE[state];
  if (state.startsWith('wf-')) {
    if (state.endsWith('executing') || state.endsWith('running')) return 'warn';
    if (state.endsWith('failed')) return 'danger';
    if (state.endsWith('completed') || state.endsWith('done')) return 'ok';
  }
  return 'muted';
}

/** Display labels for canonical states — no "legacy" duplicates. */
const STATE_LABEL: Record<string, string> = {
  registered: 'registered',
  blocked: 'blocked',
  executing: 'executing',
  awaiting_review: 'awaiting review',
  done: 'done',
  merged: 'merged',
  validated: 'validated',
  failed: 'failed',
  cancelled: 'cancelled',
  mergeable: 'mergeable',
  'blocked-on-conflict': 'blocked · conflict',
  'blocked-on-ci': 'blocked · ci',
  'blocked-on-review': 'blocked · review',
  'blocked-on-validate': 'blocked · validate',
  'needs-more-info': 'needs info',
};

const HEIGHTS = { sm: 18, md: 22, lg: 26 } as const;
const FONTS = { sm: 10.5, md: 11.5, lg: 12.5 } as const;
const PADS = { sm: 6, md: 8, lg: 10 } as const;
type BadgeSize = keyof typeof HEIGHTS;

interface StateBadgeProps {
  state: string | null | undefined;
  /** Tooltip text — shown on hover. For danger badges, also surfaces a `?` glyph. */
  why?: string;
  size?: BadgeSize;
  /** Optional inline glyph (icon) rendered between the dot and the label. */
  glyph?: ReactNode;
  style?: CSSProperties;
}

export function StateBadge({ state, why, size = 'md', style, glyph }: StateBadgeProps) {
  const tone = toneOf(state);
  const label = STATE_LABEL[state ?? ''] ?? (state ?? '').replace(/_/g, ' ');
  const h = HEIGHTS[size];
  const fs = FONTS[size];
  const px = PADS[size];
  return (
    <span
      title={why || label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        height: h,
        padding: `0 ${px}px`,
        borderRadius: 4,
        fontFamily: 'var(--tm-mono)',
        fontSize: fs,
        fontWeight: 500,
        letterSpacing: 0.2,
        textTransform: 'lowercase',
        background: `var(--tm-${tone}-bg)`,
        color: `var(--tm-${tone}-fg)`,
        border: `1px solid var(--tm-${tone}-edge)`,
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: 999,
          background: `var(--tm-${tone})`,
          animation:
            tone === 'warn' || tone === 'danger'
              ? 'tm-pulse-soft 1.8s ease-in-out infinite'
              : 'none',
        }}
      />
      {glyph && <span style={{ display: 'inline-flex' }}>{glyph}</span>}
      {label}
      {tone === 'danger' && why && (
        <span style={{ marginLeft: 2, opacity: 0.7, fontSize: fs - 1 }}>?</span>
      )}
    </span>
  );
}

/** Exported for tests + for any caller that needs to render the label string itself. */
export { STATE_LABEL };
