/**
 * App routing tests — ADR-0070 substep 2 redirect validation.
 *
 * Tests verify that the legacy `/triage` path redirects to
 * `/review/triage-finding` and that the framework page chrome renders
 * with the new viewer instead of the legacy TriageLabeling component.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import type { PropsWithChildren } from 'react';

vi.mock('./api/review_queries', () => ({
  useReviewNext: () => ({
    data: [],
    isLoading: false,
    error: null,
  }),
  useReviewStats: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
  useLabelReviewRow: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  }),
}));

import { App } from './App';

function Providers({
  initialRoute,
  children,
}: PropsWithChildren & { initialRoute: string }) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialRoute]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe('App routing — /triage redirect', () => {
  it('redirects /triage to /review/triage-finding and renders framework chrome', async () => {
    render(
      <Providers initialRoute="/triage">
        <App />
      </Providers>,
    );

    await waitFor(() => {
      // Framework page chrome: the FlipThroughLayout renders a title
      expect(screen.getByText(/review · triage-finding/i)).toBeInTheDocument();
    });
  });

  it('does not render legacy TriageLabeling heading', async () => {
    render(
      <Providers initialRoute="/triage">
        <App />
      </Providers>,
    );

    // Wait for the redirect to complete
    await waitFor(() => {
      expect(screen.getByText(/review · triage-finding/i)).toBeInTheDocument();
    });

    // Verify legacy heading is absent
    expect(screen.queryByText(/triage · labeling/i)).not.toBeInTheDocument();
  });
});
