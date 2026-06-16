/**
 * Step transcripts — the conversation for JUST one loop step.
 *
 * A worker session is one long transcript spanning many steps. Each
 * `llm_calls` row carries `task_execution_id` + `request_id` + the
 * `transcript_path`; a step is therefore the slice of transcript lines
 * whose `requestId` is in that execution's request-id set (bounded
 * first-match → last-match). These fixtures are real slices, extracted
 * by that join (see tools-side extractor), bundled so the drill-in shows
 * genuine content. The live path replaces this import with an endpoint
 * that does the same slice on demand.
 */

export type StepTurnKind = 'say' | 'tool' | 'result';

export interface StepTurn {
  kind: StepTurnKind;
  /** say: assistant prose. result: tool output (truncated). */
  text?: string;
  /** tool: tool name (Bash / Read / …). */
  name?: string;
  /** tool: one-line hint — the command / file / pattern. */
  hint?: string;
  ts?: string;
  truncated?: boolean;
}

export interface StepTranscript {
  taskExecutionId: string;
  title: string;
  kind: string;
  actor: string;
  model: string;
  calls: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  turnCount: number;
  turns: StepTurn[];
}

// Eager-bundle every extracted step slice. Keyed by task_execution_id.
const modules = import.meta.glob<StepTranscript>('../fixtures/steps/*.json', {
  eager: true,
  import: 'default',
});

export const STEP_TRANSCRIPTS: Record<string, StepTranscript> = {};
for (const path in modules) {
  const m = modules[path] as StepTranscript;
  STEP_TRANSCRIPTS[m.taskExecutionId] = m;
}

export function stepTranscript(stepId?: string): StepTranscript | undefined {
  return stepId ? STEP_TRANSCRIPTS[stepId] : undefined;
}
