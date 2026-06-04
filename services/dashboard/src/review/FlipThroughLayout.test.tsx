/**
 * FlipThroughLayout — shared chrome smoke + space-to-accept regression
 * (ADR-0070's one-keystroke confirm path).
 */
import type { ReactNode } from 'react';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import { FlipThroughLayout } from './FlipThroughLayout';
import type { ReviewKindViewer, ReviewRow } from './types';

const STUB_VIEWER: ReviewKindViewer = ({ row }) => (
  <div data-testid="viewer">row::{row.id}</div>
);

function makeRow(overrides: Partial<ReviewRow<unknown, string>> = {}): ReviewRow<
  unknown,
  string
> {
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

function withRouter(node: ReactNode) {
  return <MemoryRouter>{node}</MemoryRouter>;
}

function press(key: string) {
  act(() => {
    window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  });
}

describe('FlipThroughLayout', () => {
  it('shows the empty-queue copy when row is null and not loading', () => {
    render(
      withRouter(
        <FlipThroughLayout
          title="review · architect-gold"
          row={null}
          onLabel={vi.fn()}
          remaining={0}
          viewer={STUB_VIEWER}
          loading={false}
          error={null}
          stats={null}
        />,
      ),
    );
    expect(screen.getByText('// queue empty')).toBeInTheDocument();
    expect(screen.getByText('nothing to label here')).toBeInTheDocument();
  });

  it('renders the per-kind viewer body for a row', () => {
    render(
      withRouter(
        <FlipThroughLayout
          title="review · architect-gold"
          row={makeRow({ id: 'row_xyz_42' })}
          onLabel={vi.fn()}
          remaining={5}
          viewer={STUB_VIEWER}
          loading={false}
          error={null}
          stats={null}
        />,
      ),
    );
    expect(screen.getByTestId('viewer')).toHaveTextContent('row::row_xyz_42');
  });

  it('invokes onLabel with the LLM recommendation on space (one-keystroke confirm)', () => {
    const onLabel = vi.fn();
    render(
      withRouter(
        <FlipThroughLayout
          title="review · architect-gold"
          row={makeRow({ llm: { ...makeRow().llm, label: 'too-permissive' } })}
          onLabel={onLabel}
          remaining={5}
          viewer={STUB_VIEWER}
          loading={false}
          error={null}
          stats={null}
        />,
      ),
    );
    press(' ');
    expect(onLabel).toHaveBeenCalledTimes(1);
    expect(onLabel).toHaveBeenCalledWith({
      label: 'too-permissive',
      labeled_by: 'operator',
    });
  });

  it('surfaces the error message in the PageLayout error panel', () => {
    render(
      withRouter(
        <FlipThroughLayout
          title="review · architect-gold"
          row={null}
          onLabel={vi.fn()}
          remaining={0}
          viewer={STUB_VIEWER}
          loading={false}
          error={new Error('boom')}
          stats={null}
        />,
      ),
    );
    expect(screen.getByText('boom')).toBeInTheDocument();
  });
});
