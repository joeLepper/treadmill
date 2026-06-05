/**
 * TriageLabeling — page test pinning the connection-freshness affordance.
 *
 * Regression for triage finding 300648e9: the `/triage` page was rendering
 * without a `<ConnectionAffordance>` in the top bar, violating DESIGN.md
 * mandatory rule #8 ("connection-freshness affordance always visible").
 * The fix wires `useLiveSim()` into the page and threads its mode +
 * lastUpdated through `freshness={…}` on `<PageLayout>`. This test
 * mocks the sim and asserts the "Live" affordance text reaches the DOM.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { PropsWithChildren } from 'react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../api/sim', () => ({
  useLiveSim: () => ({
    tick: 0,
    mode: 'ws' as const,
    lastUpdated: '12:34:56',
    flashIds: new Set<string>(),
  }),
}));

vi.mock('../api/queries', () => ({
  useUnlabeledFindings: () => ({
    data: [],
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useLabelFinding: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  }),
}));

import { TriageLabeling } from './TriageLabeling';

function Providers({ children }: PropsWithChildren) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/triage']}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe('TriageLabeling', () => {
  it('renders the ConnectionAffordance in the top bar (finding 300648e9)', () => {
    render(
      <Providers>
        <TriageLabeling />
      </Providers>,
    );
    expect(screen.getByText(/Live/i)).toBeInTheDocument();
  });
});
