/**
 * <ConfidenceStrip> — shared header strip for ADR-0070 review queues.
 *
 * Three buckets (high / medium / low) showing how the operator's
 * labeling effort is distributing today (labeled_today) and what's left
 * in each bucket (queue_remaining). Optional accuracy pill displays the
 * per-kind `label_accuracy` returned by `GET /stats` so the operator
 * can see the cybernetic loop closing in real time.
 *
 * Pure presentation — no hooks, no fetches. The hosting page feeds it
 * data from `useReviewStats(kind)`.
 */

import type { ReviewConfidence } from './types';

export interface ConfidenceCount {
  confidence: ReviewConfidence;
  labeled_today: number;
  queue_remaining: number;
}

export interface ConfidenceStripProps {
  counts: ConfidenceCount[];
  accuracyToday?: number | null;
}

const BUCKET_ORDER: ReviewConfidence[] = ['high', 'medium', 'low'];

export function ConfidenceStrip({ counts, accuracyToday }: ConfidenceStripProps) {
  const byBucket = new Map<ReviewConfidence, ConfidenceCount>();
  for (const c of counts) byBucket.set(c.confidence, c);
  const accuracyPct =
    accuracyToday === null || accuracyToday === undefined
      ? null
      : Math.round(accuracyToday * 100);

  return (
    <div
      style={{
        display: 'flex',
        gap: 12,
        alignItems: 'stretch',
        padding: '10px 12px',
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
      }}
    >
      {BUCKET_ORDER.map((bucket) => {
        const c = byBucket.get(bucket) ?? {
          confidence: bucket,
          labeled_today: 0,
          queue_remaining: 0,
        };
        return <Bucket key={bucket} count={c} />;
      })}
      <div style={{ flex: 1 }} />
      {accuracyPct !== null && (
        <div
          aria-label="label accuracy today"
          style={{
            alignSelf: 'center',
            padding: '4px 10px',
            border: '1px solid var(--tm-border-2)',
            borderRadius: 2,
            background: 'var(--tm-surface-2)',
            fontFamily: 'var(--tm-mono)',
            fontSize: 11.5,
            color: 'var(--tm-t1)',
            letterSpacing: 0.4,
          }}
        >
          <span style={{ color: 'var(--tm-t4)' }}>accuracy</span>{' '}
          <span className="tm-tnum">{accuracyPct}%</span>
        </div>
      )}
    </div>
  );
}

function Bucket({ count }: { count: ConfidenceCount }) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        padding: '4px 12px',
        borderRight: '1px solid var(--tm-border)',
        minWidth: 110,
      }}
    >
      <span
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 10,
          color: 'var(--tm-t4)',
          letterSpacing: 1.2,
          textTransform: 'uppercase',
        }}
      >
        {count.confidence}
      </span>
      <div
        style={{
          display: 'flex',
          gap: 10,
          fontFamily: 'var(--tm-mono)',
          fontSize: 11.5,
          color: 'var(--tm-t2)',
        }}
      >
        <span>
          <span style={{ color: 'var(--tm-t4)' }}>labeled</span>{' '}
          <span className="tm-tnum" style={{ color: 'var(--tm-t1)' }}>
            {count.labeled_today}
          </span>
        </span>
        <span>
          <span style={{ color: 'var(--tm-t4)' }}>queue</span>{' '}
          <span className="tm-tnum" style={{ color: 'var(--tm-t1)' }}>
            {count.queue_remaining}
          </span>
        </span>
      </div>
    </div>
  );
}
