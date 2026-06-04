/**
 * <Lifecycle> — compact horizontal stepper, "where in the canonical
 * journey is this task?"
 *
 * Per DESIGN.md mandatory rule #4: every detail page has a lifecycle
 * visualization above the fold. The C-direction segmented-bar variant
 * sits above the iteration track on Task Detail (the iteration track
 * answers a different question: how many times has it looped?).
 *
 * Failure surfaces inside the same component, not as a separate state —
 * the *current* segment turns danger-toned with an X glyph rather than
 * the stepper disappearing.
 */

import { Check, X } from 'lucide-react';

const LIFECYCLE = [
  { key: 'registered', label: 'Registered' },
  { key: 'executing', label: 'Executing' },
  { key: 'awaiting_review', label: 'Review' },
  { key: 'merged', label: 'Merged' },
  { key: 'validated', label: 'Validated' },
] as const;

/** Map a `derived_status` value to its 0-indexed position on the lifecycle. */
export function deriveLifecycleIdx(status: string | null | undefined): number {
  if (status === 'validated') return 4;
  if (status === 'merged' || status === 'done') return 3;
  if (status === 'awaiting_review' || status === 'mergeable') return 2;
  if (status === 'executing' || (status && status.startsWith('blocked'))) return 1;
  if (status === 'failed' || status === 'cancelled') return 1;
  if (status && (status.startsWith('pr_opened') || status.includes('(wf-'))) return 1;
  return 0;
}

export function Lifecycle({ status }: { status: string | null | undefined }) {
  const idx = deriveLifecycleIdx(status);
  const isFailed = status === 'failed' || (status ?? '').startsWith('blocked');
  return (
    <section
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        overflow: 'hidden',
        background: 'var(--tm-surface)',
        display: 'flex',
      }}
    >
      {LIFECYCLE.map((s, i) => {
        const isPast = i < idx;
        const isCur = i === idx;
        let bg = 'transparent';
        let fg = 'var(--tm-t4)';
        if (isPast) {
          bg = 'var(--tm-ok-bg)';
          fg = 'var(--tm-ok-fg)';
        } else if (isCur && isFailed) {
          bg = 'var(--tm-danger-bg)';
          fg = 'var(--tm-danger-fg)';
        } else if (isCur) {
          bg = 'var(--tm-warn-bg)';
          fg = 'var(--tm-warn-fg)';
        }
        return (
          <div
            key={s.key}
            style={{
              flex: 1,
              padding: '8px 14px',
              background: bg,
              color: fg,
              borderLeft: i === 0 ? 'none' : '1px solid var(--tm-border)',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontFamily: 'var(--tm-mono)',
              fontSize: 11,
              letterSpacing: 1.2,
              textTransform: 'uppercase',
              fontWeight: isCur ? 600 : 400,
            }}
          >
            <span style={{ opacity: 0.55, fontWeight: 400 }}>
              {String(i + 1).padStart(2, '0')}
            </span>
            {isPast && <Check size={10} strokeWidth={2.5} />}
            {isCur && !isFailed && (
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: 999,
                  background: 'var(--tm-warn)',
                  animation: 'tm-pulse-soft 1.6s ease-in-out infinite',
                }}
              />
            )}
            {isCur && isFailed && <X size={10} strokeWidth={2.5} />}
            <span>{s.label}</span>
            {!isPast && !isCur && (
              <span style={{ marginLeft: 'auto', opacity: 0.4 }}>·</span>
            )}
          </div>
        );
      })}
    </section>
  );
}

export { LIFECYCLE };
