/**
 * TriageFindingViewer — test suite for triage-finding review queue viewer.
 *
 * Tests cover: evidence rendering, LLM card, accept/reject/skip paths,
 * and draft reset on row change.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import TriageFindingViewer from './triage-finding';
import type { ReviewRow } from '../types';
import type { TriageFinding } from '../../api/types';

function makeRow(
  overrides: Partial<TriageFinding> = {},
): ReviewRow<TriageFinding, string> {
  const base: TriageFinding = {
    finding_id: 'f1234567890abcdef',
    run_id: 'run_test',
    created_at: '2026-01-01T00:00:00Z',
    prompt_version: 'v1.0',
    model: 'claude-3-sonnet',
    mode: 'periodic',
    on_demand_request: null,
    target_url: 'https://example.com',
    viewport_w: 1280,
    viewport_h: 720,
    git_sha: 'abc123def456',
    api_git_sha: null,
    screenshot_uri: 'https://example.com/screenshot.png',
    viewport_png_uri: null,
    dom_snapshot_uri: null,
    console_log_uri: 'https://example.com/console.log',
    network_log_uri: 'https://example.com/network.log',
    evidence_summary: {},
    category: 'console_error',
    severity: 'high',
    confidence: 'high',
    observation: 'Found a console error on page load',
    evidence_pointer: 'console.log shows TypeError: x is undefined',
    proposed_resolution: 'Fix the undefined variable in src/app.js',
    dispatch_action: 'dispatched',
    dispatch_reason: 'high severity error',
    suppression_signal: null,
    parent_finding_id: null,
    dispatched_plan_id: null,
    outcome_state: null,
    outcome_pr_number: null,
    outcome_merged_at: null,
    recurrence_count: 0,
    label_is_real_bug: null,
    label_severity: null,
    label_category: null,
    label_fix_in_dsl: null,
    label_dispatch_action: null,
    label_notes: null,
    labeled_by: null,
    labeled_at: null,
    label_guidelines_version: null,
    ...overrides,
  };

  return {
    id: 'row_test',
    created_at: '2026-01-01T00:00:00Z',
    source_url: null,
    source_pr_number: null,
    candidate: base,
    llm: {
      label: 'true',
      confidence: 'high',
      rationale: 'This is a real bug that should be fixed',
      prompt_version: 'v1.0',
      model: 'claude-3-sonnet',
    },
  };
}

describe('TriageFindingViewer', () => {
  it('renders evidence fields (observation, evidence_pointer, proposed_resolution)', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    expect(screen.getByText(/Found a console error on page load/)).toBeInTheDocument();
    expect(screen.getByText(/console.log shows TypeError/)).toBeInTheDocument();
    expect(screen.getByText(/Fix the undefined variable/)).toBeInTheDocument();
  });

  it('renders screenshot from HTTP URI', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    const img = screen.getByAltText(/Found a console error on page load/);
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute('src', 'https://example.com/screenshot.png');
  });

  it('renders LLM card with confidence and rationale', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    expect(screen.getByText(/llm recommendation/i)).toBeInTheDocument();
    expect(screen.getByText(/high/)).toBeInTheDocument();
    expect(screen.getByText(/This is a real bug that should be fixed/)).toBeInTheDocument();
  });

  it('accept path: calls onLabel with label="true"', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    const yesButton = screen.getAllByRole('button').find((b) => b.textContent === 'Yes');
    fireEvent.click(yesButton!);

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label).toBe('true');
    expect(call.labeled_by).toBe('operator');
  });

  it('reject path: calls onLabel with label="false"', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    const noButton = screen.getAllByRole('button').find((b) => b.textContent === 'No');
    fireEvent.click(noButton!);

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label).toBe('false');
  });

  it('skip path: calls onLabel with label="null"', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    // Don't click Yes or No — leave as Skip
    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label).toBe('null');
  });

  it('includes kind-specific fields in onLabel call', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    // Set severity to high
    const severityHighButton = screen.getAllByRole('button').find(
      (b) => b.textContent === 'high',
    );
    fireEvent.click(severityHighButton!);

    // Set category to console_error
    const categorySelect = screen.getByDisplayValue('— skip —');
    fireEvent.change(categorySelect, { target: { value: 'console_error' } });

    // Set fix_in_dsl to Yes
    const fixInDslButtons = Array.from(screen.getAllByRole('button')).filter(
      (b) => b.textContent === 'Yes',
    );
    fireEvent.click(fixInDslButtons[1]!); // Second Yes button is for fix_in_dsl

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label_severity).toBe('high');
    expect(call.label_category).toBe('console_error');
    expect(call.label_fix_in_dsl).toBe(true);
  });

  it('resets draft when row changes', () => {
    const { rerender } = render(
      <TriageFindingViewer row={makeRow()} onLabel={vi.fn()} />,
    );

    // Set is_real_bug to Yes
    const yesButton = screen.getAllByRole('button').find((b) => b.textContent === 'Yes');
    fireEvent.click(yesButton!);

    // Change the row (different id)
    const newRow = makeRow({
      finding_id: 'f_different',
    });
    newRow.id = 'row_different';
    rerender(<TriageFindingViewer row={newRow} onLabel={vi.fn()} />);

    // Verify Skip is selected again (draft was reset)
    const skipButtons = Array.from(screen.getAllByRole('button')).filter(
      (b) => b.textContent === 'Skip',
    );
    expect(skipButtons[0]).toHaveStyle({
      background: 'var(--tm-t1)',
    });
  });

  it('includes notes in override_reason and notes fields', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<TriageFindingViewer row={row} onLabel={onLabel} />);

    const notesInput = screen.getByPlaceholderText(
      /optional — free-form context for the labeler/,
    );
    fireEvent.change(notesInput, {
      target: { value: 'Found during manual testing' },
    });

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.notes).toBe('Found during manual testing');
    expect(call.override_reason).toBe('Found during manual testing');
  });
});
