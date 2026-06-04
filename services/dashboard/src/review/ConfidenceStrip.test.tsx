/**
 * ConfidenceStrip — bucket rendering + optional accuracy pill.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ConfidenceStrip, type ConfidenceCount } from './ConfidenceStrip';

const COUNTS: ConfidenceCount[] = [
  { confidence: 'high', labeled_today: 7, queue_remaining: 12 },
  { confidence: 'medium', labeled_today: 3, queue_remaining: 21 },
  { confidence: 'low', labeled_today: 1, queue_remaining: 34 },
];

describe('ConfidenceStrip', () => {
  it('renders all three buckets with their counts', () => {
    render(<ConfidenceStrip counts={COUNTS} />);

    expect(screen.getByText('high')).toBeInTheDocument();
    expect(screen.getByText('medium')).toBeInTheDocument();
    expect(screen.getByText('low')).toBeInTheDocument();

    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('21')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('34')).toBeInTheDocument();
  });

  it('renders the accuracy pill when accuracyToday is provided', () => {
    render(<ConfidenceStrip counts={COUNTS} accuracyToday={0.87} />);
    expect(screen.getByLabelText(/label accuracy today/i)).toBeInTheDocument();
    expect(screen.getByText('87%')).toBeInTheDocument();
  });

  it('does not render the accuracy pill when accuracyToday is null', () => {
    render(<ConfidenceStrip counts={COUNTS} accuracyToday={null} />);
    expect(screen.queryByLabelText(/label accuracy today/i)).not.toBeInTheDocument();
  });

  it('does not render the accuracy pill when accuracyToday is omitted', () => {
    render(<ConfidenceStrip counts={COUNTS} />);
    expect(screen.queryByLabelText(/label accuracy today/i)).not.toBeInTheDocument();
  });
});
