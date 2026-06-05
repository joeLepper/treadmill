/**
 * Query hooks for the ADR-0070 per-kind review surfaces.
 *
 * Separate from `queries.ts` so the per-kind review-queue plumbing
 * doesn't pollute the page-aggregation hooks that the rest of the
 * dashboard consumes. Every hook is parameterised by `kind` so the same
 * three signatures cover every viewer the registry resolves.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  ReviewLabelInput,
  ReviewRow,
} from '../review/types';
import type { StatsResponse } from './review_types';

const NEXT_STALE_MS = 3_000;
const STATS_STALE_MS = 15_000;

async function _apiFetch<T>(url: string): Promise<T> {
  const res = await fetch(url, {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export function reviewNextKey(kind: string) {
  return ['review', kind, 'next'] as const;
}

export function reviewStatsKey(kind: string) {
  return ['review', kind, 'stats'] as const;
}

export function useReviewNext(kind: string, opts: { limit?: number } = {}) {
  const limit = opts.limit ?? 20;
  return useQuery({
    queryKey: reviewNextKey(kind),
    queryFn: async () =>
      _apiFetch<ReviewRow<unknown, string>[]>(
        `/api/v1/review/${kind}/next?limit=${limit}`,
      ),
    staleTime: NEXT_STALE_MS,
  });
}

export function useReviewStats(kind: string) {
  return useQuery({
    queryKey: reviewStatsKey(kind),
    queryFn: async () =>
      _apiFetch<StatsResponse>(`/api/v1/review/${kind}/stats`),
    staleTime: STATS_STALE_MS,
  });
}

export function useLabelReviewRow(kind: string) {
  const qc = useQueryClient();
  const NEXT_KEY = reviewNextKey(kind);
  const STATS_KEY = reviewStatsKey(kind);

  return useMutation({
    mutationFn: async ({
      id,
      label,
    }: {
      id: string;
      label: ReviewLabelInput;
    }) => {
      const res = await fetch(`/api/v1/review/${kind}/${id}/label`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(label),
      });
      if (!res.ok) {
        throw new Error(`label review row failed: HTTP ${res.status}`);
      }
      return (await res.json()) as ReviewRow<unknown, string>;
    },
    // Optimistic: drop the labeled row out of the unlabeled cache so the
    // chrome flips to the next row without waiting for a refetch. Mirrors
    // `useLabelFinding` in queries.ts.
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: NEXT_KEY });
      const prev = qc.getQueryData<ReviewRow<unknown, string>[]>(NEXT_KEY);
      qc.setQueryData<ReviewRow<unknown, string>[] | undefined>(
        NEXT_KEY,
        (old) => old?.filter((r) => r.id !== id),
      );
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(NEXT_KEY, ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: STATS_KEY });
    },
  });
}
