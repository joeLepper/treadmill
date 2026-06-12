/**
 * App shell — routes.
 *
 * Dashboard v2 (post-ADR-0087) lands on Mission Control. The v1 Overview +
 * the DSPy review queues are retained at /legacy + /review/* during the
 * transition (the brief's kill-list removes them once v2 is the operator
 * default).
 */

import { Navigate, Route, Routes } from 'react-router-dom';
import { Overview } from './pages/Overview';
import { ReviewKind } from './pages/ReviewKind';
import DspyVariantPrReview from './review/dspy_variant_pr';
import { MissionControl } from './pages/MissionControl';
import { LoopPipeline } from './pages/LoopPipeline';
import { TaskLoop } from './pages/TaskLoop';
import { CostPerOutcome } from './pages/CostPerOutcome';
import { AdrLedger } from './pages/AdrLedger';
import { PlanLedger } from './pages/PlanLedger';
import { PlanDetail } from './pages/PlanDetail';
import { DocDetail } from './pages/DocDetail';
import { EscalationsV2 } from './pages/EscalationsV2';

export function App() {
  return (
    <Routes>
      {/* v2 — the post-ADR-0087 operator surface */}
      <Route path="/" element={<MissionControl />} />
      <Route path="/tasks" element={<LoopPipeline />} />
      <Route path="/tasks/:taskId" element={<TaskLoop />} />
      <Route path="/loop" element={<Navigate to="/tasks" replace />} />
      <Route path="/cost" element={<CostPerOutcome />} />
      <Route path="/adrs" element={<AdrLedger />} />
      <Route path="/adrs/:docId" element={<DocDetail />} />
      <Route path="/plans" element={<PlanLedger />} />
      <Route path="/plans/:planId" element={<PlanDetail />} />
      <Route path="/drafts" element={<Navigate to="/plans" replace />} />
      <Route path="/escalations" element={<EscalationsV2 />} />

      {/* v1 — retained during transition (brief kill-list) */}
      <Route path="/legacy" element={<Overview />} />
      <Route path="/review/dspy-variant-pr" element={<DspyVariantPrReview />} />
      <Route path="/review/:kind" element={<ReviewKind />} />
      <Route path="/triage" element={<Navigate to="/review/triage-finding" replace />} />

      {/* Fallback — bounce unknown routes back to mission control. */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
