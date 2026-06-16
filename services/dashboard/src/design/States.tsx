/**
 * Loading / empty / error states — the ONLY things a data surface shows
 * when it doesn't have live data. Per the 2026-06-12 rule, a UI never
 * substitutes mock/fabricated data; it shows that it's loading, empty, or
 * errored instead.
 */

export function Loading({ label = 'loading', rows = 4 }: { label?: string; rows?: number }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, letterSpacing: 0.5, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}…</div>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} style={{ height: 34, background: 'var(--tm-surface)', border: '1px solid var(--tm-border)', borderRadius: 2, animation: 'tm-pulse-soft 1.6s ease-in-out infinite', opacity: 0.6 - i * 0.08 }} />
      ))}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div style={{ padding: 24, textAlign: 'center', fontFamily: 'var(--tm-mono)', fontSize: 12, color: 'var(--tm-t4)' }}>
      {message}
    </div>
  );
}

export function ErrorState({ error, what }: { error: unknown; what: string }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div style={{ border: '1px solid var(--tm-danger-edge)', background: 'var(--tm-danger-bg)', color: 'var(--tm-danger-fg)', padding: 14, borderRadius: 2, fontFamily: 'var(--tm-mono)', fontSize: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>// couldn't load {what}</div>
      <div style={{ color: 'var(--tm-t3)' }}>{msg}</div>
    </div>
  );
}
