/**
 * Per-kind review-queue hook tests (ADR-0070 substep 1.4).
 *
 * Mirrors the shape of `queries.test.tsx`: fetch is stubbed globally
 * and each hook renders inside a fresh `QueryClientProvider`
 * (retries disabled). The optimistic-update assertions on
 * `useLabelReviewRow` mirror `useLabelFinding` — drop the labeled row
 * out of the unlabeled cache so the chrome advances without a refetch.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { PropsWithChildren } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  reviewNextKey,
  useLabelReviewRow,
  useReviewNext,
  useReviewStats,
} from './review_queries';
import type { ReviewRow } from '../review/types';

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, Wrapper };
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

function makeRow(
  overrides: Partial<ReviewRow<unknown, string>> = {},
): ReviewRow<unknown, string> {
  return {
    id: 'row_001',
    created_at: '2026-06-04T00:00:00Z',
    source_url: null,
    source_pr_number: null,
    candidate: { foo: 'bar' },
    llm: {
      label: 'correct',
      confidence: 'high',
      rationale: 'looks right',
      prompt_version: 'v1',
      model: 'opus-4-7',
    },
    ...overrides,
  };
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useReviewNext', () => {
  it('GETs /api/v1/review/<kind>/next with the default limit', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse([]));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useReviewNext('architect-gold'), {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/review/architect-gold/next?limit=20',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
  });

  it('threads the limit through the URL when set', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse([]));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(
      () => useReviewNext('validator-gold', { limit: 5 }),
      { wrapper: Wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/review/validator-gold/next?limit=5',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
  });

  it('surfaces non-2xx as a thrown error', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response('nope', { status: 503, statusText: 'Service Unavailable' }),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useReviewNext('architect-gold'), {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as Error).message).toMatch(/503/);
  });
});

describe('useReviewStats', () => {
  it('GETs /api/v1/review/<kind>/stats', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        total: 0,
        unlabeled: 0,
        labeled_total: 0,
        label_accuracy: null,
        accuracy_last_100: null,
      }),
    );

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useReviewStats('architect-gold'), {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/review/architect-gold/stats',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
  });
});

describe('useLabelReviewRow', () => {
  it('POSTs to /api/v1/review/<kind>/<id>/label with the ReviewLabelInput body', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse(makeRow({ id: 'row_xyz' })));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useLabelReviewRow('architect-gold'), {
      wrapper: Wrapper,
    });

    await act(async () => {
      await result.current.mutateAsync({
        id: 'row_xyz',
        label: {
          label: 'correct',
          override_reason: null,
          notes: null,
          labeled_by: 'operator',
        },
      });
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/review/architect-gold/row_xyz/label');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual({
      label: 'correct',
      override_reason: null,
      notes: null,
      labeled_by: 'operator',
    });
  });

  it('optimistically drops the labeled row from the next cache', async () => {
    const fetchMock = vi.mocked(fetch);
    let resolveFetch!: (r: Response) => void;
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((r) => {
        resolveFetch = r;
      }),
    );

    const { qc, Wrapper } = makeWrapper();
    const seed: ReviewRow<unknown, string>[] = [
      makeRow({ id: 'row_a' }),
      makeRow({ id: 'row_b' }),
      makeRow({ id: 'row_c' }),
    ];
    qc.setQueryData(reviewNextKey('architect-gold'), seed);

    const { result } = renderHook(() => useLabelReviewRow('architect-gold'), {
      wrapper: Wrapper,
    });

    act(() => {
      void result.current.mutate({
        id: 'row_a',
        label: { label: 'correct', labeled_by: 'operator' },
      });
    });

    await waitFor(() => {
      const mid = qc.getQueryData<ReviewRow<unknown, string>[]>(
        reviewNextKey('architect-gold'),
      )!;
      expect(mid.map((r) => r.id)).toEqual(['row_b', 'row_c']);
    });

    await act(async () => {
      resolveFetch(jsonResponse(makeRow({ id: 'row_a' })));
    });
  });

  it('rolls back the cache on fetch failure', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(new Response('boom', { status: 500 }));

    const { qc, Wrapper } = makeWrapper();
    const seed: ReviewRow<unknown, string>[] = [
      makeRow({ id: 'row_a' }),
      makeRow({ id: 'row_b' }),
    ];
    qc.setQueryData(reviewNextKey('architect-gold'), seed);

    const { result } = renderHook(() => useLabelReviewRow('architect-gold'), {
      wrapper: Wrapper,
    });

    let err: unknown;
    await act(async () => {
      try {
        await result.current.mutateAsync({
          id: 'row_a',
          label: { label: 'correct', labeled_by: 'operator' },
        });
      } catch (e) {
        err = e;
      }
    });

    expect((err as Error).message).toMatch(/500/);
    await waitFor(() => {
      const after = qc.getQueryData<ReviewRow<unknown, string>[]>(
        reviewNextKey('architect-gold'),
      )!;
      expect(after.map((r) => r.id)).toEqual(['row_a', 'row_b']);
    });
  });
});
