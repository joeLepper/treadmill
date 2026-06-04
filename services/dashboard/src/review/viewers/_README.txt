Per-kind viewers for ADR-0070 review queues live here.

The registry (`../registry.ts`) discovers every `*.tsx` file in this
directory via `import.meta.glob` (eager) and keys the result by the
filename stem. So `architect-gold.tsx` registers as kind
`architect-gold` and is served at `/review/architect-gold`.

Contract for a viewer module:

  // viewers/<kind>.tsx
  import type { ReviewKindViewer } from '../types';

  const Viewer: ReviewKindViewer<MyCandidate, MyLabel> = ({ row, onLabel }) => {
    // Render `row.candidate` (per-kind columns) on the left.
    // Render `row.llm` (recommendation + confidence + rationale) on
    // the right with a labeled card.
    // Call `onLabel({label, override_reason?, notes?, labeled_by})`
    // when the operator commits a label.
    // Optionally listen for `review:request-override-focus` on
    // `window` to move focus to your override-reason field when the
    // operator presses `x`.
    return ...;
  };
  export default Viewer;

This file (`_README.txt`) is intentionally NOT a `.tsx` — the registry's
glob pattern only matches `*.tsx`, so this README stays out of the
discovered set.

Substep 1.3 of ADR-0070 ships only the substrate. The first real viewer
(architect-gold) lands in substep 2.
