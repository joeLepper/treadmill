/**
 * ActionBar — regression guards for PR-B10.
 *
 * B7's audit (`docs/dashboard/validate-override-surface.md`) concluded that
 * `validate.override` has no callable HTTP surface and the previous render
 * condition conflated validate.override with review.override. The button was
 * removed in B10; these tests pin the removal so a future revert doesn't
 * silently reintroduce an action with no backing endpoint.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ActionBar } from './TaskDetail';
import type { PullRequest, Task } from '../api/types';

const basePR: PullRequest = {
  pr_number: 980,
  branch: 'claude/feature',
  head_sha: 'abc1234',
  ci_conclusion: 'success',
  review_decision: null,
  validate_decision: null,
  pr_conflicting: false,
  derived_mergeability: 'pending',
};

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'tsk_test0001',
    title: 'test task',
    repo: 'osmo/web',
    repo_mode: 'conform',
    account: 'osmo',
    plan_id: 'pln_test',
    derived_status: 'wf-quick: executing',
    last_activity: new Date(),
    started_at: new Date(),
    created_at: new Date(),
    pipeline: [],
    workflow: 'wf-quick',
    pr: null,
    escalated: false,
    cost_usd: 0,
    tokens: 0,
    ...overrides,
  };
}

describe('ActionBar', () => {
  it('does not render an override·review button when review_decision is changes_requested', () => {
    const task = makeTask({
      pr: { ...basePR, review_decision: 'changes_requested' },
    });
    render(<ActionBar task={task} onCancel={vi.fn()} onAck={vi.fn()} />);
    expect(screen.queryByText(/override·review/)).not.toBeInTheDocument();
  });

  it('renders ack·escalation when the task is escalated', () => {
    const task = makeTask({ escalated: true });
    render(<ActionBar task={task} onCancel={vi.fn()} onAck={vi.fn()} />);
    expect(screen.getByText(/ack·escalation/)).toBeInTheDocument();
  });
});
