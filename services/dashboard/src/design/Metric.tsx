/**
 * <Metric>, <MetricCell>, <Age>, <Caret> — the typed-inline-value primitives.
 *
 * Ported from the Claude Design handoff bundle (treadmill-format.jsx).
 * Always tabular-nums + monospace, so columns of numbers visually align.
 * Tone (color) only ever comes from `tones.*` — never hand-rolled at call
 * sites — so semantic color stays consistent across pages.
 */

import type { CSSProperties } from 'react';
import { fmt, type FmtKind, type Tone, tones } from './fmt';

const SIZES = { sm: 11, md: 12.5, lg: 16, xl: 22 } as const;
export type MetricSize = keyof typeof SIZES;

interface MetricProps {
  kind: FmtKind;
  value: unknown;
  tone?: Tone | null;
  sub?: string;
  size?: MetricSize;
  style?: CSSProperties;
}

export function Metric({ kind, value, tone, sub, size = 'md', style }: MetricProps) {
  const formatter = fmt[kind] as (v: unknown) => string;
  // Both "should never happen" and "use raw for unfamiliar types"
  const display = formatter ? formatter(value as never) : fmt.raw(value);
  return (
    <span
      className="tm-tnum"
      style={{
        fontFamily: 'var(--tm-mono)',
        fontSize: SIZES[size],
        fontWeight: size === 'xl' ? 500 : 400,
        color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)',
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {display}
      {sub && (
        <span style={{ color: 'var(--tm-t4)', marginLeft: 4, fontWeight: 400 }}>{sub}</span>
      )}
    </span>
  );
}

interface MetricCellProps extends MetricProps {
  label: string;
  align?: 'left' | 'right';
}

export function MetricCell({
  label,
  kind,
  value,
  tone,
  sub,
  size = 'md',
  align = 'left',
  style,
}: MetricCellProps) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
        alignItems: align === 'right' ? 'flex-end' : 'flex-start',
        ...style,
      }}
    >
      <span
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 9.5,
          color: 'var(--tm-t4)',
          letterSpacing: 0.8,
          textTransform: 'uppercase',
          whiteSpace: 'nowrap',
        }}
      >
        {label}
      </span>
      <Metric kind={kind} value={value} tone={tone} sub={sub} size={size} />
    </div>
  );
}

/**
 * Common case: a Date you want "Xm ago" for. Pass `context` to derive tone:
 * - "in-flight" — >10m danger, >3m warn
 * - "blocked"   — >10m danger, >1m warn  (blocked seconds matter more)
 */
export function Age({
  date,
  context = null,
  suffix = 'ago',
  size = 'md',
  style,
}: {
  date: Date | string | null | undefined;
  context?: 'in-flight' | 'blocked' | null;
  suffix?: string;
  size?: MetricSize;
  style?: CSSProperties;
}) {
  if (!date) return <span style={{ color: 'var(--tm-t4)' }}>—</span>;
  const seconds = Math.max(
    0,
    Math.floor((Date.now() - new Date(date).getTime()) / 1000),
  );
  const tone: Tone | null =
    context === 'in-flight'
      ? tones.ageInFlight(seconds)
      : context === 'blocked'
        ? tones.ageBlocked(seconds)
        : null;
  return (
    <Metric kind="duration" value={seconds} tone={tone} sub={suffix} size={size} style={style} />
  );
}

/** Blinking █ for a tail -f feel at the foot of a live feed. */
export function Caret({ char = '█', color = 'var(--tm-t2)' }: { char?: string; color?: string }) {
  return (
    <span
      style={{
        display: 'inline-block',
        color,
        fontFamily: 'var(--tm-mono)',
        animation: 'tm-caret-blink 1.1s steps(1, end) infinite',
      }}
    >
      {char}
    </span>
  );
}
