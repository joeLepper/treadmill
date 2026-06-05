/**
 * DspyVariantPrReview component tests (ADR-0070 substep 4.3).
 *
 * Mirrors the wrapper/mocking pattern from src/api/queries.test.tsx:
 * fetch is stubbed globally; each case wraps the component in a fresh
 * QueryClientProvider + MemoryRouter so hooks resolve against the mock.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { PropsWithChildren } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import DspyVariantPrReview from './dspy_variant_pr';
import type { DspyVariantPrRow } from '../api/types';

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
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

const BASE_ROW: DspyVariantPrRow = {
  id: 'row-001',
  created_at: '2026-06-01T12:00:00Z',
  source_run_id: 'run-abc',
  source_pr_number: 42,
  source_pr_url: 'https://github.com/org/repo/pull/42',
  judge_role: 'role-ci-analyzer',
  judge_prompt_path: 'prompts/ci_analyzer.md',
  current_score: 0.820,
  variant_score: 0.893,
  improvement: 0.073,
  patch_diff: '--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new',
  corpus_s3_uri: 's3://treadmill-corpus/ci-analyzer/run-abc.jsonl',
  llm_label: 'merge',
  llm_confidence: 'high',
  llm_rationale: 'Variant shows clear improvement across all test cases.',
  llm_prompt_version: 'v2.1',
  llm_model: 'claude-opus-4-7',
  label_verdict: null,
  label_notes: null,
  label_override_reason: null,
  labeled_by: null,
  labeled_at: null,
  label_guidelines_version: null,
  outcome_state: null,
  outcome_merged_at: null,
};

const STATS_RESPONSE = {
  total: 5,
  unlabeled: 1,
  labeled_total: 4,
  label_accuracy: 0.92,
  accuracy_last_100: 0.95,
};

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('DspyVariantPrReview', () => {
  it('renders queue header + first row when data is present', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/next')) return Promise.resolve(jsonResponse([BASE_ROW]));
      if (url.includes('/stats')) return Promise.resolve(jsonResponse(STATS_RESPONSE));
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    const { Wrapper } = makeWrapper();
    render(<DspyVariantPrReview />, { wrapper: Wrapper });

    await waitFor(() => {
      expect(screen.getByText('role-ci-analyzer')).toBeInTheDocument();
    });
    expect(screen.getByText('PR #42')).toBeInTheDocument();
  });

  it('renders the empty state when the queue is empty', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/next')) return Promise.resolve(jsonResponse([]));
      if (url.includes('/stats'))
        return Promise.resolve(
          jsonResponse({
            total: 0,
            unlabeled: 0,
            labeled_total: 0,
            label_accuracy: null,
            accuracy_last_100: null,
          }),
        );
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    const { Wrapper } = makeWrapper();
    render(<DspyVariantPrReview />, { wrapper: Wrapper });

    await waitFor(() => {
      expect(screen.getByText(/queue empty/)).toBeInTheDocument();
    });
    expect(
      screen.getByText(/No unlabeled DSPy variant PR candidates/),
    ).toBeInTheDocument();
  });

  it('submits a label with override_reason when the operator disagrees', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if ((init?.method ?? 'GET') === 'POST' && url.includes('/label')) {
        return Promise.resolve(jsonResponse({ ...BASE_ROW, label_verdict: 'drop' }));
      }
      if (url.includes('/next')) return Promise.resolve(jsonResponse([BASE_ROW]));
      if (url.includes('/stats')) return Promise.resolve(jsonResponse(STATS_RESPONSE));
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    const { Wrapper } = makeWrapper();
    render(<DspyVariantPrReview />, { wrapper: Wrapper });

    // Wait for the row to render.
    await waitFor(() =>
      expect(screen.getByText('role-ci-analyzer')).toBeInTheDocument(),
    );

    // Click "drop" — disagrees with llm_label='merge'.
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /^drop$/i }));
    });

    // The override_reason textarea should now appear.
    await waitFor(() => {
      expect(
        screen.getByPlaceholderText(/why does your verdict differ/),
      ).toBeInTheDocument();
    });

    // Type an override reason.
    act(() => {
      fireEvent.change(
        screen.getByPlaceholderText(/why does your verdict differ/),
        { target: { value: 'scores are misleading — context matters' } },
      );
    });

    // Submit.
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /submit/i }));
    });

    await waitFor(() => {
      const labelCall = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url) === '/api/v1/review/dspy-variant-pr/row-001/label' &&
          init?.method === 'POST',
      );
      expect(labelCall).toBeDefined();
      const body = JSON.parse(labelCall![1]?.body as string);
      expect(body.label_verdict).toBe('drop');
      expect(body.labeled_by).toBe('operator');
      expect(body.label_override_reason).toBe(
        'scores are misleading — context matters',
      );
    });
  });

  it('blocks submit without override_reason when verdict disagrees with LLM', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/next')) return Promise.resolve(jsonResponse([BASE_ROW]));
      if (url.includes('/stats')) return Promise.resolve(jsonResponse(STATS_RESPONSE));
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    const { Wrapper } = makeWrapper();
    render(<DspyVariantPrReview />, { wrapper: Wrapper });

    await waitFor(() =>
      expect(screen.getByText('role-ci-analyzer')).toBeInTheDocument(),
    );

    // Click "drop" — disagrees with llm_label='merge', no override_reason.
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /^drop$/i }));
    });

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText(/why does your verdict differ/),
      ).toBeInTheDocument();
    });

    // Submit button must be disabled (override_reason is required but empty).
    const submitBtn = screen.getByRole('button', { name: /submit/i });
    expect(submitBtn).toBeDisabled();
  });
});
