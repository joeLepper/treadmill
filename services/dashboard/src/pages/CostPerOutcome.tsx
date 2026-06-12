/**
 * Cost Per Outcome (S3) — the company-facing hero.
 *
 * North-star: COST PER MERGED OUTCOME = window spend / pr_merged count.
 * All live: /cost/rollup gives the daily token series (by model), the
 * by-model split, and the merged-outcome count; /llm_calls/report gives
 * per-session spend + cache economics. Tokens are priced client-side via
 * the pricing table. No mock — loading / empty / error only.
 */

import { Layers } from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { Metric } from '../design/Metric';
import { Area, HBar } from '../design/Charts';
import { EmptyState } from '../design/States';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import { useCostEconomics, useCostRollup, type LabelSpend } from '../api/v2queries';
import { PRICING_NOTE } from '../api/v2mock';

export function CostPerOutcome() {
  const eco = useCostEconomics(14);
  const rollup = useCostRollup(14);

  const loading = eco.isLoading || rollup.isLoading;
  const error = eco.error ?? rollup.error;

  const r = rollup.data;
  const e = eco.data;
  const windowSpend = r?.windowSpend ?? 0;
  const outcomes = r?.outcomesMerged ?? 0;
  const cpo = outcomes > 0 ? windowSpend / outcomes : 0;
  const modelMax = Math.max(1, ...(r?.byModel ?? []).map((m) => m.usd));

  return (
    <PageLayout
      title="cost per outcome"
      breadcrumb={<span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)', letterSpacing: 0.6 }}>ECONOMICS · COMPANY VIEW</span>}
      freshness={<ConnectionAffordance mode="polling" lastUpdated={new Date().toISOString()} />}
      loading={loading}
      error={error instanceof Error ? error : error ? new Error(String(error)) : null}
    >
      {r && e && (
        <>
          {/* ─── HERO BAND ───────────────────────────────────────────── */}
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(260px, 0.9fr) minmax(0, 1.6fr)', gap: 1, background: 'var(--tm-border)', border: '1px solid var(--tm-border)', borderRadius: 3, overflow: 'hidden', marginBottom: 16 }}>
            <div style={{ background: 'var(--tm-surface)', padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 14 }}>
              <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, letterSpacing: 0.8, textTransform: 'uppercase', color: 'var(--tm-t3)' }}>cost per merged outcome</span>
              <span className="tm-tnum" style={{ fontSize: 46, fontWeight: 500, letterSpacing: -1, color: 'var(--tm-t1)', lineHeight: 1 }}>{fmt.usd(cpo)}</span>
              <p style={{ margin: 0, fontSize: 11, color: 'var(--tm-t3)', lineHeight: 1.5 }}>
                {fmt.usd(windowSpend)} spend ÷ {outcomes} merged PRs over 14d. The signal to tune against — surface to the company.
              </p>
            </div>

            <div style={{ background: 'var(--tm-surface)', padding: '18px 22px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, letterSpacing: 0.8, textTransform: 'uppercase', color: 'var(--tm-t3)' }}>daily spend · {r.daily.length}d</span>
              {r.daily.length > 0 ? (
                <Area data={r.daily.map((d) => d.usd)} tone="info" height={132} />
              ) : (
                <EmptyState message="no spend in window" />
              )}
              <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-t4)' }}>
                <span>{r.daily[0]?.day ?? ''}</span><span>{r.daily[r.daily.length - 1]?.day ?? 'today'}</span>
              </div>
            </div>
          </div>

          {/* ─── ECONOMICS STAT ROW (all live) ───────────────────────── */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 1, background: 'var(--tm-border)', border: '1px solid var(--tm-border)', borderRadius: 3, overflow: 'hidden', marginBottom: 16 }}>
            <HeroStat label="window spend" value={fmt.usd(windowSpend)} sub="14d · per-model priced" />
            <HeroStat label="outcomes merged" value={String(outcomes)} sub="pr_merged · 14d" tone="ok" />
            <HeroStat label="api calls" value={e.totalCalls.toLocaleString()} sub={`${e.byLabel.length} sessions`} />
            <HeroStat label="cache read share" value={fmt.pct(e.cacheShareOfVolume)} sub={`of volume · ${fmt.pct(e.hitRatio)} hit`} tone="info" />
          </div>

          {/* Pricing-integrity banner */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 12px', marginBottom: 16, border: '1px solid var(--tm-border-2)', background: 'var(--tm-surface)', borderRadius: 2, color: 'var(--tm-t3)', fontFamily: 'var(--tm-mono)', fontSize: 10.5 }}>
            <Layers size={12} style={{ color: 'var(--tm-info-fg)' }} />
            <span>pricing · {PRICING_NOTE}</span>
            <span style={{ marginLeft: 'auto', color: 'var(--tm-ok-fg)' }}>
              harvester · {e.totalCalls.toLocaleString()} calls{e.malformedLines ? ` · ${e.malformedLines} malformed` : ''}
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', gap: 16 }}>
            {/* By model — real */}
            <Panel title="by model">
              {r.byModel.length === 0 ? <EmptyState message="no spend" /> : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {r.byModel.slice(0, 7).map((m, i) => (
                    <div key={m.model} style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, fontFamily: 'var(--tm-mono)', fontSize: 11 }}>
                        <span style={{ color: 'var(--tm-t1)' }}>{m.model}</span>
                        <span style={{ color: 'var(--tm-t4)', fontSize: 10 }}>{fmt.count(m.calls)} calls</span>
                        <Metric kind="usd" value={m.usd} size="sm" style={{ marginLeft: 'auto' }} />
                      </div>
                      <HBar value={m.usd} max={modelMax} tone={i === 0 ? 'info' : 'muted'} />
                    </div>
                  ))}
                </div>
              )}
            </Panel>

            {/* Spend by session — real */}
            <Panel title="spend by session · 14d" padded={false}>
              {e.byLabel.length === 0 ? <EmptyState message="no sessions in window" /> : (
                <SessionRows rows={e.byLabel} />
              )}
            </Panel>
          </div>
        </>
      )}
    </PageLayout>
  );
}

function SessionRows({ rows }: { rows: LabelSpend[] }) {
  return (
    <>
      {rows.slice(0, 10).map((r) => (
        <div key={r.session_label} style={{ padding: '9px 16px', borderBottom: '1px solid var(--tm-border)', display: 'flex', alignItems: 'center', gap: 12, fontFamily: 'var(--tm-mono)', fontSize: 11.5 }}>
          <span style={{ color: 'var(--tm-t1)', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: '0 1 260px' }}>{r.session_label}</span>
          <span style={{ color: 'var(--tm-t4)', fontSize: 10 }}>{r.calls.toLocaleString()} calls</span>
          <span style={{ color: 'var(--tm-info-fg)', fontSize: 10, marginLeft: 'auto' }}>{fmt.pct(r.cache_hit_ratio)} hit</span>
          <Metric kind="usd" value={r.usd} size="md" style={{ width: 70, textAlign: 'right' }} />
        </div>
      ))}
    </>
  );
}

function HeroStat({ label, value, sub, tone }: { label: string; value: string; sub: string; tone?: Tone }) {
  return (
    <div style={{ background: 'var(--tm-surface)', padding: '14px 18px', display: 'flex', flexDirection: 'column', gap: 3 }}>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
      <span className="tm-tnum" style={{ fontSize: 24, fontWeight: 500, color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)', letterSpacing: -0.5 }}>{value}</span>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>{sub}</span>
    </div>
  );
}
