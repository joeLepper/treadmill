/**
 * Registry — substep 1.3 substrate only.
 *
 * No real viewers are landed in this PR (see `viewers/_README.txt`), so
 * the registry must report an empty kind list and return null on every
 * lookup. The first viewer (`architect-gold`) lands in substep 2.
 */
import { describe, expect, it } from 'vitest';

import { getViewer, listKinds } from './registry';

describe('review registry', () => {
  it('returns null for unknown kinds', () => {
    expect(getViewer('does-not-exist')).toBeNull();
  });

  it('returns null for the in-priority kinds that have not landed yet', () => {
    // Sentinel: every kind in the ADR-0070 priority table is unregistered
    // in this substep — flipping these to non-null is what substep 2+ does.
    expect(getViewer('architect-gold')).toBeNull();
    expect(getViewer('validator-gold')).toBeNull();
    expect(getViewer('triage-finding')).toBeNull();
  });

  it('listKinds() returns an empty list until viewers are registered', () => {
    expect(listKinds()).toEqual([]);
  });
});
