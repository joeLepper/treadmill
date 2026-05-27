/**
 * <Panel> — the canonical section card.
 *
 * Console-flavored: hairline borders, monospace uppercase header. An
 * optional `accent` tone draws a 3px left edge to flag "this section
 * needs attention" (used by the blocked-issue panel on Task Detail per
 * rule D — section order driven by what's blocking progress).
 */

import type { CSSProperties, ReactNode } from 'react';
import type { Tone } from './fmt';

interface PanelProps {
  children: ReactNode;
  title?: ReactNode;
  actions?: ReactNode;
  /** Draws a 3px colored left edge — use sparingly to flag attention-needed sections. */
  accent?: Tone;
  padded?: boolean;
  style?: CSSProperties;
}

export function Panel({ children, title, actions, accent, padded = true, style }: PanelProps) {
  return (
    <section
      style={{
        background: 'transparent',
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        overflow: 'hidden',
        borderLeft: accent ? `3px solid var(--tm-${accent})` : '1px solid var(--tm-border)',
        ...style,
      }}
    >
      {(title || actions) && (
        <header
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '9px 14px',
            borderBottom: '1px solid var(--tm-border)',
            background: 'var(--tm-surface)',
          }}
        >
          <h3
            style={{
              margin: 0,
              fontSize: 11,
              fontWeight: 500,
              letterSpacing: 1,
              textTransform: 'uppercase',
              fontFamily: 'var(--tm-mono)',
              color: 'var(--tm-t2)',
              flex: 1,
            }}
          >
            {title}
          </h3>
          {actions}
        </header>
      )}
      <div style={{ padding: padded ? 14 : 0 }}>{children}</div>
    </section>
  );
}
