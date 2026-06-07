/**
 * Per-kind viewer registry for ADR-0070 review queues.
 *
 * Each kind ships `./viewers/<kind>.tsx` exporting a default
 * `ReviewKindViewer`. Vite's `import.meta.glob` (eager) collects them at
 * build time; the registry exposes a `kind → viewer` lookup the
 * `/review/<kind>` route uses to render the appropriate per-kind body.
 *
 * Substep 1.3 ships the substrate only — no real viewers are
 * registered yet, so every lookup returns `null`. Substep 2 will land
 * the first viewer (architect-gold) and the rest follow per the ADR-0070
 * priority table.
 */

import type { ReviewKindViewer } from './types';

interface ViewerModule {
  default: ReviewKindViewer;
}

const modules = import.meta.glob<ViewerModule>(
  ['./viewers/*.tsx', '!./viewers/*.test.tsx'],
  {
    eager: true,
  }
);

function stemOf(path: string): string {
  // `./viewers/architect-gold.tsx` → `architect-gold`
  const file = path.slice(path.lastIndexOf('/') + 1);
  return file.endsWith('.tsx') ? file.slice(0, -'.tsx'.length) : file;
}

const REGISTRY: Map<string, ReviewKindViewer> = (() => {
  const out = new Map<string, ReviewKindViewer>();
  for (const [path, mod] of Object.entries(modules)) {
    out.set(stemOf(path), mod.default);
  }
  return out;
})();

export function getViewer(kind: string): ReviewKindViewer | null {
  return REGISTRY.get(kind) ?? null;
}

export function listKinds(): string[] {
  return [...REGISTRY.keys()].sort();
}
