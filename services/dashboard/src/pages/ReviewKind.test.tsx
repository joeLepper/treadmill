/**
 * ReviewKind — `/review/:kind` page tests (ADR-0070 substep 1.4).
 *
 * Covers:
 *   - unknown-kind 404 fallback panel (no network)
 *   - registered-kind happy path: hooks fire, viewer renders the row
 *   - `space` keystroke routes through useReviewKeyboard → onLabel →
 *     POST /api/v1/review/<kind>/<id>/label carrying the LLM's label.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { PropsWithChildren, ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ReviewKind } from './ReviewKind';
import type { ReviewKindViewer } from '../review/types';

vi.mock('../review/registry', () => {
  const registry = new Map<string, ReviewKindViewer>();
  return {
    getViewer: (kind: string) => registry.get(kind) ?? null,
    listKinds: () => [...registry.keys()].sort(),
    __register: (kind: string, viewer: ReviewKindViewer) => {
      registry.set(kind, viewer);
    },
    __reset: () => {
      registry.clear();
    },
  };
});

// Pull the test hooks back out of the mock.
import * as registryMock from '../review/registry';
const registry = registryMock as unknown as {
  __register: (kind: string, viewer: ReviewKindViewer) => void;
  __reset: () => void;
};

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

function Providers({ children, path }: PropsWithChildren<{ path: string }>) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/review/:kind" element={children as ReactNode} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
  registry.__reset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ReviewKind', () => {
  it('renders the 404-style panel for an unknown kind and issues no fetch', async () => {
    const fetchMock = vi.mocked(fetch);
    render(
      <Providers path="/review/unknown-kind">
        <ReviewKind />
      </Providers>,
    );

    expect(
      screen.getByText(/no viewer registered for unknown-kind/),
    ).toBeInTheDocument();

    // Give react-query a tick to settle, just in case.
    await new Promise((r) => setTimeout(r, 25));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('renders the registered viewer for the first unlabeled row', async () => {
    const StubViewer: ReviewKindViewer = ({ row }) => (
      <div data-testid="stub-viewer">rendered candidate {row.id}</div>
    );
    registry.__register('_fake-kind', StubViewer);

    const row = {
      id: 'row_xyz',
      created_at: '2026-06-04T00:00:00Z',
      source_url: null,
      source_pr_number: null,
      candidate: { foo: 'bar' },
      llm: {
        label: 'correct',
        confidence: 'high',
        rationale: 'r',
        prompt_version: 'v1',
        model: 'opus-4-7',
      },
    };

    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/next')) return Promise.resolve(jsonResponse([row]));
      if (url.includes('/stats'))
        return Promise.resolve(
          jsonResponse({
            total: 1,
            unlabeled: 1,
            labeled_total: 0,
            label_accuracy: null,
            accuracy_last_100: null,
          }),
        );
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    render(
      <Providers path="/review/_fake-kind">
        <ReviewKind />
      </Providers>,
    );

    await waitFor(() =>
      expect(screen.getByTestId('stub-viewer')).toHaveTextContent(
        'rendered candidate row_xyz',
      ),
    );
  });

  it('routes space → POST /api/v1/review/<kind>/<id>/label with the LLM verdict', async () => {
    const StubViewer: ReviewKindViewer = ({ row }) => (
      <div data-testid="stub-viewer">{row.id}</div>
    );
    registry.__register('_fake-kind', StubViewer);

    const row = {
      id: 'row_xyz',
      created_at: '2026-06-04T00:00:00Z',
      source_url: null,
      source_pr_number: null,
      candidate: { foo: 'bar' },
      llm: {
        label: 'too-permissive',
        confidence: 'medium',
        rationale: 'r',
        prompt_version: 'v1',
        model: 'opus-4-7',
      },
    };

    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if ((init?.method ?? 'GET') === 'POST' && url.includes('/label')) {
        return Promise.resolve(jsonResponse(row));
      }
      if (url.includes('/next')) return Promise.resolve(jsonResponse([row]));
      if (url.includes('/stats'))
        return Promise.resolve(
          jsonResponse({
            total: 1,
            unlabeled: 1,
            labeled_total: 0,
            label_accuracy: null,
            accuracy_last_100: null,
          }),
        );
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    render(
      <Providers path="/review/_fake-kind">
        <ReviewKind />
      </Providers>,
    );

    await waitFor(() => expect(screen.getByTestId('stub-viewer')).toBeInTheDocument());

    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
    });

    await waitFor(() => {
      const labelCall = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url) === '/api/v1/review/_fake-kind/row_xyz/label' &&
          init?.method === 'POST',
      );
      expect(labelCall).toBeDefined();
      const body = JSON.parse(labelCall![1]?.body as string);
      expect(body.label).toBe('too-permissive');
      expect(body.labeled_by).toBe('operator');
    });
  });
});
