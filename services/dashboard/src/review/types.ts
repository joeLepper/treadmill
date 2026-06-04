/**
 * Shared types for ADR-0070 pre-labeled review queues.
 *
 * Every per-kind review surface (architect-gold, validator-gold,
 * triage-finding, dspy-variant-pr, …) projects its row into the
 * `ReviewRow` shape so the shared dashboard chrome
 * (`FlipThroughLayout`, `ConfidenceStrip`, keyboard handler) can drive
 * the surface without knowing the per-kind columns.
 */

import type { ReactElement } from 'react';

export type ReviewConfidence = 'high' | 'medium' | 'low';

export interface ReviewLlmRecommendation {
  label: string;
  confidence: ReviewConfidence;
  rationale: string;
  prompt_version: string;
  model: string;
}

export interface ReviewLabelInput {
  label: string;
  override_reason?: string | null;
  notes?: string | null;
  labeled_by: string;
}

/**
 * One row in a review queue. `TCandidate` carries the per-kind typed
 * candidate columns; `TLlm` is a phantom string-union that lets per-kind
 * viewers narrow `llm.label` to that kind's verdict enum.
 */
export interface ReviewRow<TCandidate, TLlm extends string> {
  id: string;
  created_at: string;
  source_url?: string | null;
  source_pr_number?: number | null;
  candidate: TCandidate;
  llm: ReviewLlmRecommendation & { label: TLlm };
}

export interface ReviewKindViewerProps<TCandidate, TLlm extends string> {
  row: ReviewRow<TCandidate, TLlm>;
  onLabel: (input: ReviewLabelInput) => void;
}

export type ReviewKindViewer<
  TCandidate = unknown,
  TLlm extends string = string,
> = (props: ReviewKindViewerProps<TCandidate, TLlm>) => ReactElement;
