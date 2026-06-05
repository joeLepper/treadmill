/**
 * ArchitectGoldViewer — test suite for architect-gold review queue viewer.
 *
 * Tests cover: candidate rendering, LLM card, label selection, and submission.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ArchitectGoldViewer from './architect-gold';
import type { ReviewRow } from './types';
import type { ArchitectGoldLabel, ArchitectGoldRow } from '../api/types';

function makeRow(
  overrides: Partial<ArchitectGoldRow> = {},
): ReviewRow<ArchitectGoldRow, ArchitectGoldLabel> {
  const base: ArchitectGoldRow = {
    id: 'row_arch_test',
    created_at: '2026-01-01T00:00:00Z',
    source_url: null,
    source_pr_number: null,
    decision_id: 'arch-decision-abc123',
    verdict_emitted: 'accept-as-is',
    rationale_excerpt: 'The change follows all guidelines and passes tests',
    gate_log_uri: 's3://example/gates/arch-decision-abc123.log',
    llm_label: 'correct',
    llm_confidence: 'high',
    llm_rationale: 'The architect correctly accepted this decision as compliant with specs',
    llm_prompt_version: 'v1',
    llm_model: 'claude-sonnet-4-6',
    label_verdict: null,
    label_notes: null,
    label_override_reason: null,
    labeled_by: null,
    labeled_at: null,
    label_guidelines_version: null,
    outcome_state: null,
    outcome_merged_at: null,
    ...overrides,
  };

  return {
    id: 'row_arch_test',
    created_at: '2026-01-01T00:00:00Z',
    source_url: null,
    source_pr_number: null,
    candidate: base,
    llm: {
      label: 'correct' as ArchitectGoldLabel,
      confidence: 'high',
      rationale: 'The architect correctly accepted this decision as compliant with specs',
      prompt_version: 'v1',
      model: 'claude-sonnet-4-6',
    },
  };
}

describe('ArchitectGoldViewer', () => {
  it('renders decision_id, verdict_emitted, and rationale_excerpt', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    // Check visible ID (first 8 chars)
    expect(screen.getByText(/arch-dec/)).toBeInTheDocument();
    expect(screen.getByText(/The change follows all guidelines/)).toBeInTheDocument();
  });

  it('renders LLM recommendation card with label, confidence, and rationale', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    expect(screen.getByText(/llm recommendation/i)).toBeInTheDocument();
    expect(screen.getByText(/high confidence/)).toBeInTheDocument();
    expect(
      screen.getByText(/The architect correctly accepted this decision/),
    ).toBeInTheDocument();
  });

  it('renders all verdict options as buttons', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    expect(screen.getByRole('button', { name: /too-permissive/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /too-strict/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /correct/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /exclude/ })).toBeInTheDocument();
  });

  it('clicking "correct" button and submit calls onLabel with label="correct"', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    const correctButton = screen.getByRole('button', { name: /correct/ });
    fireEvent.click(correctButton);

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label).toBe('correct');
    expect(call.labeled_by).toBe('operator');
  });

  it('clicking "too-permissive" button and submit calls onLabel with correct label', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    const button = screen.getByRole('button', { name: /too-permissive/ });
    fireEvent.click(button);

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label).toBe('too-permissive');
  });

  it('includes override_reason in submission when provided', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    const correctButton = screen.getByRole('button', { name: /correct/ });
    fireEvent.click(correctButton);

    const overrideInput = screen.getByPlaceholderText(/why you disagree with the LLM/);
    fireEvent.change(overrideInput, {
      target: { value: 'Actually this was too permissive' },
    });

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.override_reason).toBe('Actually this was too permissive');
  });

  it('includes notes in submission when provided', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    const correctButton = screen.getByRole('button', { name: /correct/ });
    fireEvent.click(correctButton);

    const notesInputs = screen.getAllByPlaceholderText(/free-form context/);
    fireEvent.change(notesInputs[notesInputs.length - 1], {
      target: { value: 'This decision was carefully reviewed' },
    });

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.notes).toBe('This decision was carefully reviewed');
  });

  it('shows alert when submitting without selecting a label', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ArchitectGoldViewer row={row} onLabel={onLabel} />);

    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(alertSpy).toHaveBeenCalledWith('Please select a label');
    expect(onLabel).not.toHaveBeenCalled();

    alertSpy.mockRestore();
  });

  it('resets draft when row changes', () => {
    const { rerender } = render(
      <ArchitectGoldViewer row={makeRow()} onLabel={vi.fn()} />,
    );

    const correctButton = screen.getByRole('button', { name: /correct/ });
    fireEvent.click(correctButton);

    // Verify it's selected
    expect(correctButton).toHaveStyle({
      background: 'var(--tm-t1)',
    });

    // Change the row
    const newRow = makeRow({
      decision_id: 'arch-decision-xyz789',
    });
    newRow.id = 'row_arch_different';
    rerender(<ArchitectGoldViewer row={newRow} onLabel={vi.fn()} />);

    // Verify the selection was reset (button is no longer active)
    const newCorrectButton = screen.getByRole('button', { name: /correct/ });
    expect(newCorrectButton).not.toHaveStyle({
      background: 'var(--tm-t1)',
    });
  });
});
