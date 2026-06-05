/**
 * App shell — routes.
 *
 * v1 covers two routes: Overview and Task Detail. The bunkhouse dashboard
 * has ~25; per DESIGN.md v1 deliberately only ships the two highest-
 * frequency operator surfaces. Lift more pages from bunkhouse as the
 * need is felt, not pre-emptively.
 */

import { Navigate, Route, Routes } from 'react-router-dom';
import { Overview } from './pages/Overview';
import { ReviewKind } from './pages/ReviewKind';
import { TaskDetail } from './pages/TaskDetail';
import { TriageLabeling } from './pages/TriageLabeling';

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Overview />} />
      <Route path="/tasks/:taskId" element={<TaskDetail />} />
      <Route path="/triage" element={<TriageLabeling />} />
      {/* MUST come before the wildcard or unknown /review/* paths get
          bounced to "/" instead of reaching ReviewKind's in-page
          unknown-kind fallback. */}
      <Route path="/review/:kind" element={<ReviewKind />} />
      {/* Fallback — bounce unknown routes back to the overview. */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
