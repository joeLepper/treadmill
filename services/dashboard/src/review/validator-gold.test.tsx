/**
 * ValidatorGoldViewer — test suite for validator-gold review queue viewer.
 *
 * Tests cover: candidate rendering, LLM card, label selection, and submission.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ValidatorGoldViewer from './validator-gold';
import type { ReviewRow } from './types';
import type { ValidatorGoldLabel, ValidatorGoldRow } from '../api/types';

function makeRow(
  overrides: Partial<ValidatorGoldRow> = {},
): ReviewRow<ValidatorGoldRow, ValidatorGoldLabel> {
  const base: ValidatorGoldRow = {
    id: 'row_val_test',
    created_at: '2026-01-01T00:00:00Z',
    source_url: null,
    source_pr_number: null,
    source_step_id: 'step-12345678-abcd',
    verdict_emitted: 'pass',
    script_excerpt: '#!/bin/bash\nset -e\necho "Running tests"\nnpm test',
    artifact_excerpt: 'PASS: all 42 tests passed in 12.5s',
    llm_label: 'correct-verdict',
    llm_confidence: 'high',
    llm_rationale: 'The validator correctly called pass because all tests succeeded',
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
    id: 'row_val_test',
    created_at: '2026-01-01T00:00:00Z',
    source_url: null,
    source_pr_number: null,
    candidate: base,
    llm: {
      label: 'correct-verdict' as ValidatorGoldLabel,
      confidence: 'high',
      rationale: 'The validator correctly called pass because all tests succeeded',
      prompt_version: 'v1',
      model: 'claude-sonnet-4-6',
    },
  };
}

describe('ValidatorGoldViewer', () => {
  it('renders source_step_id, verdict_emitted, script and artifact excerpts', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    // Check visible ID (first 8 chars)
    expect(screen.getByText(/step-1234/)).toBeInTheDocument();
    expect(screen.getByText(/Running tests/)).toBeInTheDocument();
    expect(screen.getByText(/all 42 tests passed/)).toBeInTheDocument();
  });

  it('renders LLM recommendation card with label, confidence, and rationale', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    expect(screen.getByText(/llm recommendation/i)).toBeInTheDocument();
    expect(screen.getByText(/high confidence/)).toBeInTheDocument();
    expect(
      screen.getByText(/The validator correctly called pass/),
    ).toBeInTheDocument();
  });

  it('renders all verdict options as buttons', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    expect(screen.getByRole('button', { name: /correct-verdict/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /wrong-verdict/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /unclear/ })).toBeInTheDocument();
  });

  it('clicking "correct-verdict" button and submit calls onLabel with label="correct-verdict"', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    const correctButton = screen.getByRole('button', { name: /correct-verdict/ });
    fireEvent.click(correctButton);

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label).toBe('correct-verdict');
    expect(call.labeled_by).toBe('operator');
  });

  it('clicking "wrong-verdict" button and submit calls onLabel with correct label', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    const button = screen.getByRole('button', { name: /wrong-verdict/ });
    fireEvent.click(button);

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.label).toBe('wrong-verdict');
  });

  it('includes override_reason in submission when provided', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    const correctButton = screen.getByRole('button', { name: /correct-verdict/ });
    fireEvent.click(correctButton);

    const overrideInput = screen.getByPlaceholderText(/why you disagree with the LLM/);
    fireEvent.change(overrideInput, {
      target: { value: 'Actually the test suite has flaky tests' },
    });

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.override_reason).toBe('Actually the test suite has flaky tests');
  });

  it('includes notes in submission when provided', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    const correctButton = screen.getByRole('button', { name: /correct-verdict/ });
    fireEvent.click(correctButton);

    const notesInputs = screen.getAllByPlaceholderText(/free-form context/);
    fireEvent.change(notesInputs[notesInputs.length - 1], {
      target: { value: 'Manual review confirms all tests passed successfully' },
    });

    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(onLabel).toHaveBeenCalledOnce();
    const call = onLabel.mock.calls[0]![0];
    expect(call.notes).toBe('Manual review confirms all tests passed successfully');
  });

  it('shows alert when submitting without selecting a label', () => {
    const row = makeRow();
    const onLabel = vi.fn();

    render(<ValidatorGoldViewer row={row} onLabel={onLabel} />);

    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    const submitButton = screen.getByText(/submit & next/);
    fireEvent.click(submitButton);

    expect(alertSpy).toHaveBeenCalledWith('Please select a label');
    expect(onLabel).not.toHaveBeenCalled();

    alertSpy.mockRestore();
  });

  it('resets draft when row changes', () => {
    const { rerender } = render(
      <ValidatorGoldViewer row={makeRow()} onLabel={vi.fn()} />,
    );

    const correctButton = screen.getByRole('button', { name: /correct-verdict/ });
    fireEvent.click(correctButton);

    // Verify it's selected
    expect(correctButton).toHaveStyle({
      background: 'var(--tm-t1)',
    });

    // Change the row
    const newRow = makeRow({
      source_step_id: 'step-87654321-dcba',
    });
    newRow.id = 'row_val_different';
    rerender(<ValidatorGoldViewer row={newRow} onLabel={vi.fn()} />);

    // Verify the selection was reset (button is no longer active)
    const newCorrectButton = screen.getByRole('button', { name: /correct-verdict/ });
    expect(newCorrectButton).not.toHaveStyle({
      background: 'var(--tm-t1)',
    });
  });
});
