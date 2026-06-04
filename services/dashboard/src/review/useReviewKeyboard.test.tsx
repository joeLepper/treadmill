/**
 * useReviewKeyboard — closed shortcut set (space/x/s/?/j/k) plus the
 * editable-element guard.
 */
import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useReviewKeyboard, type KeyHandlers } from './useReviewKeyboard';

function makeHandlers(): KeyHandlers {
  return {
    onAccept: vi.fn(),
    onReject: vi.fn(),
    onSkip: vi.fn(),
    onHelp: vi.fn(),
    onNext: vi.fn(),
    onPrev: vi.fn(),
  };
}

function Harness({
  handlers,
  enabled = true,
  withInput = false,
}: {
  handlers: KeyHandlers;
  enabled?: boolean;
  withInput?: boolean;
}) {
  useReviewKeyboard(handlers, enabled);
  return withInput ? <input data-testid="notes" /> : null;
}

function press(key: string) {
  act(() => {
    window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  });
}

afterEach(() => {
  // Defensive: ensure no stray focus carries over between tests.
  if (document.activeElement instanceof HTMLElement) {
    document.activeElement.blur();
  }
});

describe('useReviewKeyboard', () => {
  it('maps each shortcut to its handler', () => {
    const h = makeHandlers();
    render(<Harness handlers={h} />);

    press(' ');
    expect(h.onAccept).toHaveBeenCalledTimes(1);
    press('x');
    expect(h.onReject).toHaveBeenCalledTimes(1);
    press('s');
    expect(h.onSkip).toHaveBeenCalledTimes(1);
    press('?');
    expect(h.onHelp).toHaveBeenCalledTimes(1);
    press('j');
    expect(h.onNext).toHaveBeenCalledTimes(1);
    press('k');
    expect(h.onPrev).toHaveBeenCalledTimes(1);
  });

  it('ignores keystrokes when an <input> has focus (notes guard)', () => {
    const h = makeHandlers();
    const { getByTestId } = render(<Harness handlers={h} withInput />);
    const input = getByTestId('notes') as HTMLInputElement;
    input.focus();
    expect(document.activeElement).toBe(input);

    press(' ');
    press('x');
    press('s');
    expect(h.onAccept).not.toHaveBeenCalled();
    expect(h.onReject).not.toHaveBeenCalled();
    expect(h.onSkip).not.toHaveBeenCalled();
  });

  it('does nothing when enabled is false', () => {
    const h = makeHandlers();
    render(<Harness handlers={h} enabled={false} />);

    press(' ');
    press('x');
    press('?');
    expect(h.onAccept).not.toHaveBeenCalled();
    expect(h.onReject).not.toHaveBeenCalled();
    expect(h.onHelp).not.toHaveBeenCalled();
  });

  it('ignores unknown keys', () => {
    const h = makeHandlers();
    render(<Harness handlers={h} />);
    press('a');
    press('Enter');
    expect(h.onAccept).not.toHaveBeenCalled();
    expect(h.onReject).not.toHaveBeenCalled();
    expect(h.onSkip).not.toHaveBeenCalled();
    expect(h.onHelp).not.toHaveBeenCalled();
    expect(h.onNext).not.toHaveBeenCalled();
    expect(h.onPrev).not.toHaveBeenCalled();
  });
});
