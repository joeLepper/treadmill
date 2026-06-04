/**
 * Global keyboard handler for the shared review chrome (ADR-0070).
 *
 * Shortcuts (closed set):
 *   space → onAccept   (confirm the LLM recommendation, single-keystroke)
 *   x     → onReject   (per-viewer override-reason field is then surfaced)
 *   s     → onSkip
 *   ?     → onHelp     (open per-kind guidelines)
 *   j     → onNext
 *   k     → onPrev
 *
 * Guard: when the focused element is an `<input>`, `<textarea>`, or a
 * `[contenteditable]`, the event is ignored so the operator can type
 * notes / override reasons without triggering shortcuts.
 */

import { useEffect } from 'react';

export interface KeyHandlers {
  onAccept: () => void;
  onReject: () => void;
  onSkip: () => void;
  onHelp: () => void;
  onNext: () => void;
  onPrev: () => void;
}

function isEditable(target: Element | null): boolean {
  if (!target) return false;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
  if (target.getAttribute && target.getAttribute('contenteditable') === 'true') {
    return true;
  }
  return false;
}

export function useReviewKeyboard(
  handlers: KeyHandlers,
  enabled: boolean = true,
): void {
  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isEditable(document.activeElement)) return;

      let handled = true;
      switch (event.key) {
        case ' ':
        case 'Spacebar':
          handlers.onAccept();
          break;
        case 'x':
        case 'X':
          handlers.onReject();
          break;
        case 's':
        case 'S':
          handlers.onSkip();
          break;
        case '?':
          handlers.onHelp();
          break;
        case 'j':
        case 'J':
          handlers.onNext();
          break;
        case 'k':
        case 'K':
          handlers.onPrev();
          break;
        default:
          handled = false;
      }
      if (handled) event.preventDefault();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [enabled, handlers]);
}
