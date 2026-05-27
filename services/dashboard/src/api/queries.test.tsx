/**
 * Phase-2 swap: verify the READ hooks call the live endpoints with the
 * right URL + query parameters, and surface fetch failures as errors.
 *
 * `fetch` is mocked globally. We render each hook inside a fresh
 * `QueryClientProvider` (retries disabled, refetchInterval off via the
 * default test config) and assert what the network layer saw.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { PropsWithChildren } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useOverview, useRepoDocs, useTaskDetail } from './queries';

function wrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return function Wrapper({ children }: PropsWithChildren) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useOverview', () => {
  it('hits /api/v1/dashboard/overview with no query string when no filters', async () => {
    const payload = {
      accounts: [],
      fleet: {},
      escalations: [],
      tasks: [],
      bucketCounts: { blocked: 0, inflight: 0, hopper: 0, total: 0 },
      events: [],
    };
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(payload),
    );

    const { result } = renderHook(() => useOverview(), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0];
    expect(url).toBe('/api/v1/dashboard/overview');
    expect(init).toMatchObject({
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    expect(result.current.data).toEqual(payload);
  });

  it('forwards repo, bucket, account, q as query parameters', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({
        accounts: [],
        fleet: {},
        escalations: [],
        tasks: [],
        bucketCounts: { blocked: 0, inflight: 0, hopper: 0, total: 0 },
        events: [],
      }),
    );

    const { result } = renderHook(
      () =>
        useOverview({
          repo: 'x',
          bucket: 'blocked',
          account: 'y',
          q: 'z',
        }),
      { wrapper: wrapper() },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe(
      '/api/v1/dashboard/overview?repo=x&bucket=blocked&account=y&q=z',
    );
  });
});

describe('useTaskDetail', () => {
  it('hits /api/v1/dashboard/tasks/<id>', async () => {
    const payload = { task: { id: 't-1' }, runs: [] };
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(payload),
    );

    const { result } = renderHook(() => useTaskDetail('t-1'), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/v1/dashboard/tasks/t-1');
    expect(result.current.data).toEqual(payload);
  });
});

describe('useRepoDocs', () => {
  it('hits /api/v1/dashboard/repos/<encoded-repo>/docs', async () => {
    const payload = { arch: 'arch.md', plans: 2, last_updated: '2026-05-27' };
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(payload),
    );

    const { result } = renderHook(() => useRepoDocs('joeLepper/treadmill'), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/v1/dashboard/repos/joeLepper%2Ftreadmill/docs');
    expect(result.current.data).toEqual(payload);
  });
});

describe('_apiFetch error surfacing', () => {
  it('throws on non-2xx responses', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('nope', { status: 503, statusText: 'Service Unavailable' }),
    );

    const { result } = renderHook(() => useTaskDetail('missing'), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(Error);
    expect((result.current.error as Error).message).toBe(
      '503 Service Unavailable',
    );
  });

  it('surfaces network failures as errors', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      new TypeError('Failed to fetch'),
    );

    const { result } = renderHook(() => useRepoDocs('foo/bar'), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(TypeError);
    expect((result.current.error as Error).message).toBe('Failed to fetch');
  });
});
