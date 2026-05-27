/**
 * Treadmill formatters — one canonical formatter per type.
 *
 * Ported verbatim from the Claude Design handoff bundle (treadmill-format.jsx).
 *
 * Observability discipline: anywhere the dashboard renders a number, it MUST
 * go through `fmt[...]` (or the `<Metric kind="...">` primitive). No inline
 * `.toFixed(2)` drift, no hand-rolled relative-time logic. This is what
 * keeps the UI's numeric vocabulary uniform across pages over time.
 */

export const fmt = {
  // ─── Money ─────────────────────────────────────────────────────────
  usd(n: number | null | undefined): string {
    if (n == null) return '—';
    if (n === 0) return '$0.00';
    const abs = Math.abs(n);
    if (abs < 0.01) return '<$0.01';
    if (abs >= 10_000) return `$${(n / 1000).toFixed(1)}k`;
    if (abs >= 1000) return `$${n.toFixed(0)}`;
    return `$${n.toFixed(2)}`;
  },

  // ─── Tokens (k / M, no decimal noise) ──────────────────────────────
  tokens(n: number | null | undefined): string {
    if (n == null) return '—';
    if (n === 0) return '0';
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
    if (n >= 100_000) return `${Math.round(n / 1000)}k`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
  },

  // ─── Counts ────────────────────────────────────────────────────────
  count(n: number | null | undefined): string {
    if (n == null) return '—';
    if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`;
    if (n >= 1000) return `${(n / 1000).toFixed(2)}k`;
    return String(n);
  },

  // ─── Duration in seconds → "12s", "4m 32s", "2h 18m", "3d 4h" ─────
  duration(s: number | null | undefined): string {
    if (s == null || isNaN(s as number)) return '—';
    let sec = Math.floor(s as number);
    if (sec < 0) sec = 0;
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    if (m < 60) {
      const rem = sec % 60;
      return rem ? `${m}m ${rem}s` : `${m}m`;
    }
    const h = Math.floor(m / 60);
    if (h < 24) {
      const remM = m % 60;
      return remM ? `${h}h ${remM}m` : `${h}h`;
    }
    const d = Math.floor(h / 24);
    return `${d}d ${h % 24}h`;
  },

  // ─── Age (Date → relative duration; no "ago" suffix; caller adds it) ─
  age(date: Date | string | null | undefined): string {
    if (!date) return '—';
    const s = Math.max(
      0,
      Math.floor((Date.now() - new Date(date).getTime()) / 1000),
    );
    return fmt.duration(s);
  },

  // ─── HH:MM:SS local clock ──────────────────────────────────────────
  time(date?: Date | string | null): string {
    const d = date ? new Date(date) : new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, '0'))
      .join(':');
  },

  // ─── Short sha (7 chars) ──────────────────────────────────────────
  sha(s: string | null | undefined, n = 7): string {
    if (!s) return '—';
    return s.slice(0, n);
  },

  // ─── Short ID (12 chars by default — task/plan ids) ───────────────
  id(s: string | null | undefined, n = 12): string {
    if (!s) return '—';
    return s.slice(0, n);
  },

  // ─── Percent ──────────────────────────────────────────────────────
  pct(n: number | null | undefined): string {
    if (n == null) return '—';
    return `${Math.round(n * 100)}%`;
  },

  // ─── Lines of code (with sign for delta) ──────────────────────────
  loc(n: number | null | undefined): string {
    if (n == null) return '—';
    return `${n >= 0 ? '+' : ''}${n.toLocaleString()}`;
  },

  // ─── Bytes (B / kB / MB / GB) ─────────────────────────────────────
  bytes(n: number | null | undefined): string {
    if (n == null) return '—';
    if (n < 1024) return `${n}B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}kB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)}GB`;
  },

  // ─── Raw passthrough — used by <Metric kind="raw"> for special values
  raw(v: unknown): string {
    return String(v ?? '—');
  },
} as const;

export type FmtKind = keyof typeof fmt;

/**
 * Tone helpers — the ONLY way to derive a semantic color from a metric value.
 * Hand-rolling `s > 600 ? "danger" : ...` at call sites is forbidden.
 */
export const tones = {
  /** Age in seconds for an in-flight task. >10m = danger, >3m = warn. */
  ageInFlight(seconds: number | null | undefined): Tone | null {
    if (seconds == null) return null;
    if (seconds > 600) return 'danger';
    if (seconds > 180) return 'warn';
    return null;
  },
  /** Age for a blocked task. Every blocked second matters more. */
  ageBlocked(seconds: number | null | undefined): Tone | null {
    if (seconds == null) return null;
    if (seconds > 600) return 'danger';
    if (seconds > 60) return 'warn';
    return null;
  },
  /** Heartbeat freshness (scheduler / autoscaler tick age in seconds). */
  heartbeat(seconds: number | null | undefined): Tone {
    if (seconds == null) return 'danger';
    if (seconds > 60) return 'danger';
    if (seconds > 30) return 'warn';
    return 'ok';
  },
} as const;

export type Tone = 'danger' | 'warn' | 'ok' | 'muted' | 'info';
