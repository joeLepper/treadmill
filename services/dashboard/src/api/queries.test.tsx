/**
 * Phase-2 swap tests — both READ and WRITE hooks now hit live endpoints
 * via `_apiFetch` against `services/api/treadmill_api/routers/dashboard/`.
 *
 * `fetch` is mocked globally. Each hook renders inside a fresh
 * `QueryClientProvider` (retries disabled) and we assert what the
 * network layer saw + that the page-visible hook shapes are unchanged.
 * The optimistic-update + rollback machinery on
 * `useAcknowledgeEscalation` was already in place before the swap; we
 * pin it here so a future rewrite doesn't quietly drop it.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { PropsWithChildren } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  useAcknowledgeEscalation,
  useCancelTask,
  useOverview,
  useRepoDocs,
  useTaskDetail,
} from './queries';

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

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
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

function okResponse(body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 202,
    headers: { 'content-type': 'application/json' },
  });
}

function errResponse(status: number): Response {
  return new Response('boom', { status });
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
      fleet: {
        workers_running: 0,
        workers_capacity: 4,
        autoscaler_last_tick: new Date(0),
        autoscaler_alive_since: new Date(0),
        scheduler_last_tick: new Date(0),
        scheduler_alive_since: new Date(0),
      },
      escalations: [],
      tasks: [],
      bucketCounts: { blocked: 0, inflight: 0, hopper: 0, total: 0 },
      events: [],
    };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(payload));

    const { result } = renderHook(() => useOverview(), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/dashboard/overview',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
  });

  it('passes filters as query parameters when set', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        accounts: [],
        fleet: {
          workers_running: 0,
          workers_capacity: 4,
          autoscaler_last_tick: new Date(0),
          autoscaler_alive_since: new Date(0),
          scheduler_last_tick: new Date(0),
          scheduler_alive_since: new Date(0),
        },
        escalations: [],
        tasks: [],
        bucketCounts: { blocked: 0, inflight: 0, hopper: 0, total: 0 },
        events: [],
      }),
    );

    const { result } = renderHook(
      () =>
        useOverview({ repo: 'foo/bar', bucket: 'blocked', account: 'osmo', q: 'auth' }),
      { wrapper: wrapper() },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toContain('?');
    expect(String(url)).toContain('repo=foo%2Fbar');
    expect(String(url)).toContain('bucket=blocked');
    expect(String(url)).toContain('account=osmo');
    expect(String(url)).toContain('q=auth');
  });

  it('surfaces non-2xx as a thrown error', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response('nope', { status: 500, statusText: 'Internal Server Error' }),
    );
    const { result } = renderHook(() => useOverview(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as Error).message).toMatch(/500/);
  });
});

describe('useTaskDetail', () => {
  it('hits /api/v1/dashboard/tasks/:id', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ task: { id: 'tsk_abc' }, runs: [] }),
    );
    const { result } = renderHook(() => useTaskDetail('tsk_abc'), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/dashboard/tasks/tsk_abc',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
  });
});

describe('useRepoDocs', () => {
  it('hits /api/v1/dashboard/repos/:repo/docs with the repo URL-encoded', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ arch: 'arch.md', plans: 3, last_updated: new Date(0) }),
    );
    const { result } = renderHook(() => useRepoDocs('foo/bar'), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/dashboard/repos/foo%2Fbar/docs',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
  });

  it('surfaces network failure as an error', async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const { result } = renderHook(() => useRepoDocs('foo/bar'), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(TypeError);
    expect((result.current.error as Error).message).toBe('Failed to fetch');
  });
});

describe('useCancelTask', () => {
  it('POSTs to the cancel endpoint with the reason in the JSON body', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(okResponse({ event_id: 'e1', task_id: 't1' }));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useCancelTask(), { wrapper: Wrapper });

    await act(async () => {
      await result.current.mutateAsync({ taskId: 'tsk_abc', reason: 'operator gave up' });
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/dashboard/tasks/tsk_abc/cancel');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual({ reason: 'operator gave up' });
  });

  it('surfaces non-2xx as a thrown error carrying the HTTP status', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(errResponse(409));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useCancelTask(), { wrapper: Wrapper });

    await expect(
      act(async () => {
        await result.current.mutateAsync({ taskId: 'tsk_abc', reason: 'x' });
      }),
    ).rejects.toThrow(/409/);
  });
});

describe('useAcknowledgeEscalation', () => {
  it('POSTs to the ack-escalation endpoint with no body', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(okResponse({ event_id: 'e1', task_id: 't1' }));

    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => useAcknowledgeEscalation(), {
      wrapper: Wrapper,
    });

    await act(async () => {
      await result.current.mutateAsync({ taskId: 'tsk_abc' });
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/dashboard/tasks/tsk_abc/ack-escalation');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeUndefined();
  });

  it('optimistically clears the escalation, then rolls back on fetch failure', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(errResponse(500));

    const { qc, Wrapper } = makeWrapper();

    const seed = {
      escalations: [
        { task_id: 'tsk_abc', repo: 'osmo/web', title: 'x', escalated_at: new Date(), reason: 'why' },
        { task_id: 'tsk_other', repo: 'osmo/web', title: 'y', escalated_at: new Date(), reason: 'why' },
      ],
      tasks: [
        { id: 'tsk_abc', escalated: true },
        { id: 'tsk_other', escalated: true },
      ],
    };
    qc.setQueryData(['overview', {}], seed);

    const { result } = renderHook(() => useAcknowledgeEscalation(), {
      wrapper: Wrapper,
    });

    let mutationError: unknown;
    await act(async () => {
      try {
        await result.current.mutateAsync({ taskId: 'tsk_abc' });
      } catch (e) {
        mutationError = e;
      }
    });

    expect(mutationError).toBeInstanceOf(Error);
    expect((mutationError as Error).message).toMatch(/500/);

    // After rollback the overview cache must match the seed exactly —
    // the optimistic filter must not have stuck.
    await waitFor(() => {
      const after = qc.getQueryData<typeof seed>(['overview', {}])!;
      expect(after.escalations.map((e) => e.task_id)).toEqual([
        'tsk_abc',
        'tsk_other',
      ]);
      expect(after.tasks.find((t) => t.id === 'tsk_abc')?.escalated).toBe(true);
    });
  });

  it('optimistic update removes the escalation from cache before the fetch resolves', async () => {
    const fetchMock = vi.mocked(fetch);
    let resolveFetch!: (r: Response) => void;
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((r) => {
        resolveFetch = r;
      }),
    );

    const { qc, Wrapper } = makeWrapper();
    qc.setQueryData(['overview', {}], {
      escalations: [
        { task_id: 'tsk_abc', repo: 'osmo/web', title: 'x', escalated_at: new Date(), reason: 'why' },
      ],
      tasks: [{ id: 'tsk_abc', escalated: true }],
    });

    const { result } = renderHook(() => useAcknowledgeEscalation(), {
      wrapper: Wrapper,
    });

    act(() => {
      void result.current.mutate({ taskId: 'tsk_abc' });
    });

    await waitFor(() => {
      const mid = qc.getQueryData<{
        escalations: { task_id: string }[];
        tasks: { id: string; escalated: boolean }[];
      }>(['overview', {}])!;
      expect(mid.escalations).toEqual([]);
      expect(mid.tasks[0].escalated).toBe(false);
    });

    await act(async () => {
      resolveFetch(okResponse({ event_id: 'e1', task_id: 'tsk_abc' }));
    });
  });
});
