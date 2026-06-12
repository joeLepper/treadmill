/**
 * useViewState — URL-as-state primitive.
 *
 * Convention (Joe 2026-06-11): if the UI can render a state, that state
 * must be reachable by URL — copy-paste / refresh / share must reproduce
 * exactly what's on screen. So ALL view state (active tab, which row is
 * expanded, which drawer is open, a filter selection) lives in the query
 * string, never in component `useState`. Identity (which plan / doc) stays
 * in the path; ephemeral view state is query params.
 *
 * This wraps react-router's useSearchParams with merge-preserving setters
 * so toggling one param never clobbers the others. A param at its default
 * is deleted, keeping URLs clean (`/plans/x` not `/plans/x?tab=execution`).
 *
 *   const v = useViewState();
 *   v.get('tab', 'execution')        // read with default
 *   v.set('tab', 'document')         // set (or clear when === default arg)
 *   v.toggle('task', t.id)           // set if different, clear if equal
 *   v.is('step', id)                 // boolean equality
 */

import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';

export interface ViewState {
  get(key: string, fallback?: string): string | undefined;
  set(key: string, value: string | null | undefined, defaultValue?: string): void;
  toggle(key: string, value: string): void;
  is(key: string, value: string): boolean;
}

export function useViewState(): ViewState {
  const [params, setParams] = useSearchParams();

  const mutate = useCallback(
    (key: string, value: string | null | undefined) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value == null || value === '') next.delete(key);
          else next.set(key, value);
          return next;
        },
        { replace: false },
      );
    },
    [setParams],
  );

  const get = useCallback(
    (key: string, fallback?: string) => params.get(key) ?? fallback,
    [params],
  );

  const set = useCallback(
    (key: string, value: string | null | undefined, defaultValue?: string) =>
      mutate(key, value != null && value === defaultValue ? null : value),
    [mutate],
  );

  const toggle = useCallback(
    (key: string, value: string) => mutate(key, params.get(key) === value ? null : value),
    [mutate, params],
  );

  const is = useCallback((key: string, value: string) => params.get(key) === value, [params]);

  return { get, set, toggle, is };
}
