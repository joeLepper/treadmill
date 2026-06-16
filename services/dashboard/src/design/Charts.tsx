/**
 * Charts — minimal SVG primitives in the direction-C vocabulary.
 *
 * No chart library: hand-rolled SVG keeps the bundle small and lets every
 * stroke/fill come from the OKLCH token set so chart color stays semantic
 * (tones only, never hand-picked hex). Used by the cost hero (S3).
 */

import type { Tone } from './fmt';

// ─── Area sparkline / trend ─────────────────────────────────────────

interface AreaProps {
  /** left→right series values */
  data: number[];
  width?: number;
  height?: number;
  tone?: Tone;
  /** optional second series rendered as a faint line (e.g. outcomes) */
  overlay?: number[];
}

export function Area({ data, width = 520, height = 120, tone = 'info', overlay }: AreaProps) {
  if (data.length === 0) return null;
  const max = Math.max(...data) * 1.12 || 1;
  const dx = width / (data.length - 1 || 1);
  const y = (v: number) => height - (v / max) * height;
  const pts = data.map((v, i) => `${i * dx},${y(v)}`);
  const linePath = `M ${pts.join(' L ')}`;
  const areaPath = `${linePath} L ${width},${height} L 0,${height} Z`;

  const omax = overlay ? Math.max(...overlay) * 1.2 || 1 : 1;
  const oPts = overlay?.map((v, i) => `${i * dx},${height - (v / omax) * height}`);

  return (
    <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: 'block', overflow: 'visible' }}>
      <defs>
        <linearGradient id={`area-${tone}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={`var(--tm-${tone})`} stopOpacity="0.28" />
          <stop offset="100%" stopColor={`var(--tm-${tone})`} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={areaPath} fill={`url(#area-${tone})`} />
      <path d={linePath} fill="none" stroke={`var(--tm-${tone}-fg)`} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      {oPts && (
        <path
          d={`M ${oPts.join(' L ')}`}
          fill="none"
          stroke="var(--tm-t3)"
          strokeWidth="1"
          strokeDasharray="3 3"
          vectorEffect="non-scaling-stroke"
        />
      )}
      {data.map((v, i) => (
        <circle key={i} cx={i * dx} cy={y(v)} r={i === data.length - 1 ? 3 : 0} fill={`var(--tm-${tone}-fg)`} />
      ))}
    </svg>
  );
}

// ─── Tiny inline sparkline ──────────────────────────────────────────

export function Spark({ data, tone = 'ok', width = 76, height = 22 }: { data: number[]; tone?: Tone; width?: number; height?: number }) {
  if (data.length === 0) return null;
  const max = Math.max(...data) || 1;
  const min = Math.min(...data);
  const dx = width / (data.length - 1 || 1);
  const y = (v: number) => height - ((v - min) / (max - min || 1)) * (height - 4) - 2;
  const pts = data.map((v, i) => `${i * dx},${y(v)}`);
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <path d={`M ${pts.join(' L ')}`} fill="none" stroke={`var(--tm-${tone}-fg)`} strokeWidth="1.25" />
      <circle cx={(data.length - 1) * dx} cy={y(data[data.length - 1])} r="2" fill={`var(--tm-${tone}-fg)`} />
    </svg>
  );
}

// ─── Horizontal stacked bar (decomposition) ─────────────────────────

export interface StackSeg {
  value: number;
  tone: Tone;
  label?: string;
}

export function StackBar({ segments, height = 10 }: { segments: StackSeg[]; height?: number }) {
  const total = segments.reduce((n, s) => n + s.value, 0) || 1;
  return (
    <div style={{ display: 'flex', height, borderRadius: 3, overflow: 'hidden', border: '1px solid var(--tm-border)' }}>
      {segments.map((s, i) => (
        <div
          key={i}
          title={s.label}
          style={{ width: `${(s.value / total) * 100}%`, background: `var(--tm-${s.tone})`, opacity: 0.85 }}
        />
      ))}
    </div>
  );
}

// ─── Horizontal bar list (by model / repo) ──────────────────────────

export function HBar({ value, max, tone = 'info', height = 6 }: { value: number; max: number; tone?: Tone; height?: number }) {
  return (
    <div style={{ height, background: 'var(--tm-surface-2)', borderRadius: 999, overflow: 'hidden' }}>
      <div style={{ width: `${Math.min(100, (value / (max || 1)) * 100)}%`, height: '100%', background: `var(--tm-${tone})`, opacity: 0.9 }} />
    </div>
  );
}
