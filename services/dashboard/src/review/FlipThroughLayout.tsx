/**
 * <FlipThroughLayout> — the shared dashboard chrome for ADR-0070
 * pre-labeled review queues.
 *
 * Generalizes the visual + interaction template from
 * `pages/TriageLabeling.tsx`: one unlabeled row at a time, header chrome
 * with queue depth + per-kind label-accuracy, body driven by a per-kind
 * `viewer` component (resolved via `registry.ts`), and a global keyboard
 * handler for the closed shortcut set documented in ADR-0070
 * (`space`/`x`/`s`/`?`/`j`/`k`).
 *
 * Hooks-as-props: stats/loading/error are passed in rather than fetched
 * here so the chrome stays trivially testable.
 *
 * Deferred (v1):
 *   - `onNext`/`onPrev` (`j`/`k`) are no-ops; the queue auto-advances on
 *     a successful label-write via the per-kind page's tanstack-query
 *     optimistic update (same shape as ADR-0061).
 *   - `skip` doesn't yet write a "__skip__" label; the chrome invokes a
 *     prop callback `onSkip` instead. Skip semantics will be wired once
 *     the corpus exporter decides how to represent skipped rows.
 */

import { useCallback } from 'react';

import { useLiveSim } from '../api/sim';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { PageLayout } from '../design/PageLayout';

import { ConfidenceStrip } from './ConfidenceStrip';
import type { ConfidenceCount } from './ConfidenceStrip';
import { useReviewKeyboard } from './useReviewKeyboard';
import type {
  ReviewKindViewer,
  ReviewLabelInput,
  ReviewRow,
} from './types';

const DEFAULT_LABELED_BY = 'operator';

/** Per-kind viewers can listen for this event to focus their override-reason field. */
export const REVIEW_REQUEST_OVERRIDE_FOCUS = 'review:request-override-focus';

export interface FlipThroughLayoutProps {
  title: string;
  row: ReviewRow<unknown, string> | null;
  onLabel: (input: ReviewLabelInput) => void;
  remaining: number;
  viewer: ReviewKindViewer;
  loading: boolean;
  error: Error | null;
  stats: {
    counts: ConfidenceCount[];
    accuracyToday: number | null;
  } | null;
  onSkip?: () => void;
  onShowHelp?: () => void;
}

export function FlipThroughLayout({
  title,
  row,
  onLabel,
  remaining,
  viewer: Viewer,
  loading,
  error,
  stats,
  onSkip,
  onShowHelp,
}: FlipThroughLayoutProps) {
  const sim = useLiveSim();

  const onAccept = useCallback(() => {
    if (!row) return;
    onLabel({ label: row.llm.label, labeled_by: DEFAULT_LABELED_BY });
  }, [row, onLabel]);

  const onReject = useCallback(() => {
    window.dispatchEvent(new CustomEvent(REVIEW_REQUEST_OVERRIDE_FOCUS));
  }, []);

  const onSkipKey = useCallback(() => {
    onSkip?.();
  }, [onSkip]);

  const onHelp = useCallback(() => {
    onShowHelp?.();
  }, [onShowHelp]);

  const noop = useCallback(() => {}, []);

  useReviewKeyboard(
    {
      onAccept,
      onReject,
      onSkip: onSkipKey,
      onHelp,
      onNext: noop,
      onPrev: noop,
    },
    Boolean(row),
  );

  return (
    <PageLayout
      title={title}
      loading={loading}
      error={error}
      freshness={<ConnectionAffordance mode={sim.mode} lastUpdated={sim.lastUpdated} />}
      breadcrumb={
        <span
          style={{
            fontSize: 10.5,
            color: 'var(--tm-t4)',
            fontFamily: 'var(--tm-mono)',
            letterSpacing: 0.8,
            textTransform: 'uppercase',
          }}
        >
          treadmill · operator
        </span>
      }
      actions={
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: 'var(--tm-t3)',
          }}
        >
          <span style={{ color: 'var(--tm-t4)' }}>queue</span>{' '}
          <span className="tm-tnum" style={{ color: 'var(--tm-t1)' }}>
            {remaining}
          </span>{' '}
          <span style={{ color: 'var(--tm-t4)' }}>unlabeled</span>
        </span>
      }
    >
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 16,
        }}
      >
        {stats && (
          <ConfidenceStrip
            counts={stats.counts}
            accuracyToday={stats.accuracyToday}
          />
        )}
        {row ? (
          <Viewer row={row} onLabel={onLabel} />
        ) : (
          <EmptyQueue />
        )}
      </div>
    </PageLayout>
  );
}

function EmptyQueue() {
  return (
    <div
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
        padding: '32px 16px',
        textAlign: 'center',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        alignItems: 'center',
        fontFamily: 'var(--tm-mono)',
        color: 'var(--tm-t3)',
        fontSize: 12,
      }}
    >
      <div style={{ fontSize: 13, color: 'var(--tm-t1)' }}>// queue empty</div>
      <div>nothing to label here</div>
    </div>
  );
}
