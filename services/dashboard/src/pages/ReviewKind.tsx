/**
 * ReviewKind — `/review/:kind` (ADR-0070).
 *
 * The single dynamic route every per-kind review queue rides. The kind
 * URL segment selects:
 *   - which viewer renders via `getViewer(kind)` (registry auto-discovery)
 *   - which backend endpoints the hooks hit (`/api/v1/review/<kind>/…`)
 *
 * Per ADR-0070, this page intentionally has no per-kind logic of its
 * own. Per-kind columns / verdict enums live in the viewer; this page
 * is just chrome + hooks + a 404 panel when no viewer is registered.
 */

import { useParams } from 'react-router-dom';

import { PageLayout } from '../design/PageLayout';
import { FlipThroughLayout } from '../review/FlipThroughLayout';
import { getViewer } from '../review/registry';
import type { ReviewKindViewer } from '../review/types';
import {
  useLabelReviewRow,
  useReviewNext,
  useReviewStats,
} from '../api/review_queries';

export function ReviewKind() {
  const { kind = '' } = useParams<{ kind: string }>();
  const viewer = getViewer(kind);

  if (!viewer) {
    return <UnknownKindPanel kind={kind} />;
  }
  return <RegisteredKind kind={kind} viewer={viewer} />;
}

function RegisteredKind({
  kind,
  viewer,
}: {
  kind: string;
  viewer: ReviewKindViewer;
}) {
  const next = useReviewNext(kind);
  const stats = useReviewStats(kind);
  const label = useLabelReviewRow(kind);

  const rows = next.data ?? [];
  const current = rows[0] ?? null;
  const remaining = rows.length;

  // The server's `StatsResponse` doesn't break out per-confidence buckets
  // yet — substep 1.4 ships the chrome with an empty `counts` array (so
  // each bucket renders zero) and surfaces `label_accuracy` as the
  // accuracy pill. Per-bucket counts land when the API grows them.
  const flipStats = stats.data
    ? {
        counts: [],
        accuracyToday: stats.data.label_accuracy,
      }
    : null;

  return (
    <FlipThroughLayout
      title={`review · ${kind}`}
      row={current}
      onLabel={(input) => {
        if (!current) return;
        label.mutate({ id: current.id, label: input });
      }}
      remaining={remaining}
      viewer={viewer}
      loading={next.isLoading}
      error={(next.error as Error | null) ?? null}
      stats={flipStats}
    />
  );
}

function UnknownKindPanel({ kind }: { kind: string }) {
  return (
    <PageLayout title={`review · ${kind || '(none)'}`}>
      <div
        style={{
          border: '1px solid var(--tm-border)',
          borderRadius: 2,
          background: 'var(--tm-surface)',
          padding: '24px 16px',
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
          fontFamily: 'var(--tm-mono)',
          color: 'var(--tm-t2)',
          fontSize: 12.5,
        }}
      >
        <div style={{ fontSize: 13, color: 'var(--tm-t1)' }}>
          // no viewer registered for {kind || '(empty)'}
        </div>
        <div style={{ color: 'var(--tm-t3)' }}>
          The review-kind registry has no entry for{' '}
          <code style={{ color: 'var(--tm-t1)' }}>{kind || '(empty)'}</code>.
        </div>
        <div style={{ color: 'var(--tm-t4)' }}>
          To register a kind, drop{' '}
          <code style={{ color: 'var(--tm-t3)' }}>
            src/review/viewers/{kind || '<kind>'}.tsx
          </code>{' '}
          exporting a default <code style={{ color: 'var(--tm-t3)' }}>ReviewKindViewer</code>.
          See <code style={{ color: 'var(--tm-t3)' }}>src/review/viewers/_README.txt</code>
          {' '}for the contract.
        </div>
      </div>
    </PageLayout>
  );
}
