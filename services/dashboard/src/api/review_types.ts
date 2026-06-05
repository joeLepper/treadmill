/**
 * Wire types for `/api/v1/review/<kind>/*` endpoints.
 *
 * Mirrors `treadmill_api.services.review_stats.StatsResponse` field-for-
 * field. The per-row shape lives in `../review/types.ts` because viewers
 * narrow it via the `ReviewRow<TCandidate, TLlm>` generics.
 */

export interface StatsResponse {
  total: number;
  unlabeled: number;
  labeled_total: number;
  label_accuracy: number | null;
  accuracy_last_100: number | null;
}
