/**
 * Per-model pricing — the config the brief flagged as load-bearing.
 *
 * USD per 1M tokens, per channel. Cache reads are ~0.1× input; getting
 * this wrong distorts every cost number on the hero, because cache reads
 * are the dominant volume. This table is the single conversion seam from
 * the harvester's real token counts (GET /api/v1/llm_calls/report) to the
 * dollars the company sees. Update rates here, nowhere else.
 *
 * Rates are illustrative current-tier placeholders; wire to a real
 * pricing source (or operator config) when one exists.
 */

export interface ModelRate {
  input: number; // $/1M input tokens
  output: number; // $/1M output tokens
  cacheRead: number; // $/1M cache-read tokens
  cacheWrite: number; // $/1M cache-creation tokens
}

const PER_M = 1_000_000;

/** Keyed by a normalized model family; falls back to opus tier. */
export const PRICING: Record<string, ModelRate> = {
  opus: { input: 15, output: 75, cacheRead: 1.5, cacheWrite: 18.75 },
  sonnet: { input: 3, output: 15, cacheRead: 0.3, cacheWrite: 3.75 },
  haiku: { input: 0.8, output: 4, cacheRead: 0.08, cacheWrite: 1 },
};

export function rateFor(model: string | null | undefined): ModelRate {
  const m = (model ?? '').toLowerCase();
  if (m.includes('haiku')) return PRICING.haiku;
  if (m.includes('sonnet')) return PRICING.sonnet;
  return PRICING.opus;
}

export interface TokenCounts {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
}

/** Convert real token counts → USD via the model rate. */
export function costOf(t: TokenCounts, model?: string | null): number {
  const r = rateFor(model);
  return (
    (t.input_tokens * r.input +
      t.output_tokens * r.output +
      t.cache_read_tokens * r.cacheRead +
      t.cache_creation_tokens * r.cacheWrite) /
    PER_M
  );
}

export const PRICING_SUMMARY = 'cache read ≈ 0.1× input · per-model · operator-configurable';
