/**
 * <ConnectionAffordance> — "is the data I'm seeing live?"
 *
 * Per DESIGN.md mandatory rule #8: every live page must show the operator
 * whether they're seeing fresh data — WebSocket-connected, polling, or
 * disconnected — and the wall-clock of the last update. Stale data must
 * never masquerade as live data.
 */

export type FreshnessMode = 'ws' | 'polling' | 'disconnected';

export function ConnectionAffordance({
  mode,
  lastUpdated,
}: {
  mode: FreshnessMode;
  lastUpdated: string;
}) {
  const tone = mode === 'ws' ? 'ok' : mode === 'polling' ? 'warn' : 'danger';
  const label =
    mode === 'ws'
      ? 'Live · WebSocket'
      : mode === 'polling'
        ? 'Polling · 30s'
        : 'Disconnected';
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 10,
        padding: '5px 10px',
        borderRadius: 2,
        background: 'transparent',
        border: '1px solid var(--tm-border)',
        fontFamily: 'var(--tm-mono)',
        fontSize: 11.5,
        color: 'var(--tm-t2)',
      }}
    >
      <span style={{ position: 'relative', width: 8, height: 8 }}>
        <span
          style={{
            position: 'absolute',
            inset: 0,
            borderRadius: 999,
            background: `var(--tm-${tone})`,
          }}
        />
        {mode === 'ws' && (
          <span
            style={{
              position: 'absolute',
              inset: 0,
              borderRadius: 999,
              background: `var(--tm-${tone})`,
              animation: 'tm-pulse-ring 2s ease-out infinite',
            }}
          />
        )}
      </span>
      <span style={{ color: `var(--tm-${tone}-fg)` }}>{label}</span>
      <span style={{ color: 'var(--tm-t4)' }}>·</span>
      <span className="tm-tnum">updated {lastUpdated}</span>
    </div>
  );
}
