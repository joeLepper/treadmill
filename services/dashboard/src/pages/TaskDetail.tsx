/**
 * TaskDetail — Treadmill operator dashboard `/tasks/:taskId`.
 *
 * Ported from the Claude Design handoff bundle (treadmill-taskdetail-v2.jsx,
 * direction C "Console v2").
 *
 * Layout hierarchy (top → bottom, left column):
 *   1. TaskHeader  — id · repo · plan · arch.md · title · account · status
 *   2. Lifecycle   — segmented 5-step bar (above the fold per DESIGN rule #4)
 *   3. Iteration track — HERO: "how many times have we looped"
 *   4. Blocking panel (only when blocked; section order driven by what's
 *      blocking, per DESIGN.md rule D)
 *   5. PR strip
 *   6. Action bar
 *   7. Iteration detail — workflow runs + steps for the selected iteration
 *
 * Right rail: cost summary · repo docs · per-task events.tail.
 */

import { type CSSProperties, type MouseEvent, type ReactNode, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  RotateCcw,
  Terminal,
  X,
  XOctagon,
} from 'lucide-react';

import { Age, Metric, MetricCell } from '../design/Metric';
import { Button } from '../design/Button';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { AccountPill, RepoCell, WorkflowChip } from '../design/chrome';
import { StateBadge } from '../design/StateBadge';
import { PageLayout } from '../design/PageLayout';
import { Lifecycle } from '../design/Lifecycle';
import { fmt } from '../design/fmt';

import {
  useAcknowledgeEscalation,
  useCancelTask,
  useRepoDocs,
  useTaskDetail,
} from '../api/queries';
import { useLiveSim } from '../api/sim';
import { deriveIterations, getEvents } from '../api/mock';
import type {
  Event,
  Iteration,
  PullRequest,
  RepoDocs,
  Run,
  RunStep,
  Task,
} from '../api/types';

/* ─── Micro label (TM caps mono) ─────────────────────────────────── */
function Micro({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <span
      style={{
        fontFamily: 'var(--tm-mono)',
        fontSize: 9.5,
        color: 'var(--tm-t4)',
        letterSpacing: 0.8,
        textTransform: 'uppercase',
        ...style,
      }}
    >
      {children}
    </span>
  );
}

/* ─── Task header — id/title/status/account; docs affordance stub ── */
function TaskHeader({ task, repoDocs }: { task: Task; repoDocs: RepoDocs | null | undefined }) {
  return (
    <header
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        padding: '12px 16px',
        background: 'var(--tm-surface)',
        display: 'grid',
        gridTemplateColumns: '1fr auto auto auto auto auto auto',
        gap: 20,
        alignItems: 'center',
      }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            fontFamily: 'var(--tm-mono)',
            fontSize: 10.5,
            color: 'var(--tm-t3)',
            letterSpacing: 0.5,
            flexWrap: 'wrap',
          }}
        >
          <span>{fmt.id(task.id)}</span>
          <span style={{ color: 'var(--tm-t4)' }}>·</span>
          <RepoCell repo={task.repo} mode={task.repo_mode} />
          <span style={{ color: 'var(--tm-t4)' }}>·</span>
          <a
            href="#"
            style={{
              color: 'var(--tm-info-fg)',
              textDecoration: 'none',
              borderBottom: '1px dotted var(--tm-border-2)',
            }}
          >
            {task.plan_id}
          </a>
          {repoDocs && (
            <>
              <span style={{ color: 'var(--tm-t4)' }}>·</span>
              <a
                href="#"
                title="Open repo arch docs · markdown + mermaid (stub)"
                style={{
                  fontFamily: 'var(--tm-mono)',
                  fontSize: 10.5,
                  color: 'var(--tm-info-fg)',
                  textDecoration: 'none',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                }}
              >
                <Terminal size={10} />
                arch.md
              </a>
            </>
          )}
        </div>
        <h2
          style={{
            margin: 0,
            fontSize: 16,
            fontWeight: 500,
            letterSpacing: 0.1,
            color: 'var(--tm-t1)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {task.title}
        </h2>
      </div>
      <MetricCell label="created" kind="age" value={task.created_at} sub="ago" align="right" />
      {task.started_at && (
        <MetricCell label="started" kind="age" value={task.started_at} sub="ago" align="right" />
      )}
      <MetricCell label="cost·task" kind="usd" value={task.cost_usd} align="right" />
      <MetricCell label="tokens" kind="tokens" value={task.tokens} align="right" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-end' }}>
        <Micro>account</Micro>
        <AccountPill name={task.account} />
      </div>
      <StateBadge state={task.derived_status} size="lg" />
    </header>
  );
}

/* ─── Iteration track — HERO: "how many times has this looped?" ───── */
function IterationNode({
  iter,
  isSelected,
  isCurrent,
  onClick,
}: {
  iter: Iteration;
  isSelected: boolean;
  isCurrent: boolean;
  onClick: () => void;
}) {
  const tone =
    iter.status === 'failed'
      ? 'danger'
      : iter.status === 'running'
        ? 'warn'
        : iter.status === 'completed'
          ? 'ok'
          : 'muted';
  const glyph =
    iter.status === 'failed'
      ? '✗'
      : iter.status === 'running'
        ? '▌'
        : iter.status === 'completed'
          ? '✓'
          : '·';

  return (
    <button
      onClick={onClick}
      style={{
        all: 'unset',
        cursor: 'pointer',
        padding: '10px 14px',
        borderRight: '1px solid var(--tm-border)',
        background: isSelected ? 'var(--tm-surface-2)' : 'transparent',
        borderTop: isCurrent
          ? `2px solid var(--tm-${tone})`
          : isSelected
            ? '2px solid var(--tm-border-2)'
            : '2px solid transparent',
        display: 'flex',
        flexDirection: 'column',
        gap: 5,
        minWidth: 130,
        transition: 'background 0.12s',
        position: 'relative',
      }}
      onMouseEnter={(e) => {
        if (!isSelected) e.currentTarget.style.background = 'var(--tm-hover)';
      }}
      onMouseLeave={(e) => {
        if (!isSelected) e.currentTarget.style.background = 'transparent';
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 7,
          justifyContent: 'space-between',
        }}
      >
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 10,
            color: 'var(--tm-t4)',
            letterSpacing: 1,
            textTransform: 'uppercase',
          }}
        >
          iter {String(iter.idx).padStart(2, '0')}
        </span>
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 13,
            color: `var(--tm-${tone}-fg)`,
            animation:
              iter.status === 'running' ? 'tm-caret-blink 1.1s steps(1, end) infinite' : 'none',
          }}
        >
          {glyph}
        </span>
      </div>
      <div
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 12,
          color: 'var(--tm-t1)',
          fontWeight: 500,
        }}
      >
        {iter.label}
      </div>
      <div
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 10,
          color: 'var(--tm-t4)',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {iter.trigger}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 2 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: 'var(--tm-t3)',
          }}
        >
          <span style={{ color: 'var(--tm-t4)' }}>dur</span>
          <Metric
            kind="duration"
            value={
              iter.duration_s != null
                ? iter.duration_s
                : iter.started_at
                  ? Math.floor((Date.now() - new Date(iter.started_at).getTime()) / 1000)
                  : null
            }
            size="sm"
            tone={iter.status === 'running' ? 'warn' : null}
          />
        </div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
          }}
        >
          <span style={{ color: 'var(--tm-t4)' }}>tok</span>
          <Metric kind="tokens" value={iter.tokens} size="sm" />
        </div>
      </div>
    </button>
  );
}

function IterationTrack({
  iterations,
  selectedIdx,
  onSelect,
}: {
  iterations: Iteration[];
  selectedIdx: number;
  onSelect: (idx: number) => void;
}) {
  const currentIdx = iterations.length;
  const totalDur = iterations.reduce(
    (a, it) =>
      a +
      (it.duration_s ||
        (it.started_at
          ? Math.floor((Date.now() - new Date(it.started_at).getTime()) / 1000)
          : 0)),
    0,
  );
  const totalTokens = iterations.reduce((a, it) => a + (it.tokens || 0), 0);
  const failureCount = iterations.filter((it) => it.status === 'failed').length;

  return (
    <section
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        overflow: 'hidden',
        background: 'var(--tm-surface)',
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          padding: '10px 16px',
          borderBottom: '1px solid var(--tm-border)',
          background: 'var(--tm-bg)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 10.5,
              letterSpacing: 1.2,
              color: 'var(--tm-t4)',
              textTransform: 'uppercase',
            }}
          >
            loop
          </span>
          <span
            className="tm-tnum"
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 24,
              fontWeight: 500,
              color: 'var(--tm-t1)',
              lineHeight: 1,
            }}
          >
            {iterations.length}
            <span style={{ color: 'var(--tm-t4)', fontSize: 14, marginLeft: 4 }}>
              iteration{iterations.length !== 1 ? 's' : ''}
            </span>
          </span>
        </div>
        <span style={{ color: 'var(--tm-t4)' }}>·</span>
        <MetricCell label="elapsed" kind="duration" value={totalDur} size="md" />
        <span style={{ color: 'var(--tm-t4)' }}>·</span>
        <MetricCell label="tokens" kind="tokens" value={totalTokens} sub="tok" size="md" />
        <span style={{ color: 'var(--tm-t4)' }}>·</span>
        <MetricCell
          label="failures"
          kind="count"
          value={failureCount}
          tone={failureCount > 0 ? 'danger' : null}
          size="md"
        />
        <span style={{ flex: 1 }} />
        {iterations.length >= 3 && (
          <span
            style={{
              fontFamily: 'var(--tm-mono)',
              fontSize: 10.5,
              color: 'var(--tm-warn-fg)',
              letterSpacing: 0.8,
              padding: '2px 7px',
              background: 'var(--tm-warn-bg)',
              border: '1px solid var(--tm-warn-edge)',
              borderRadius: 2,
              textTransform: 'uppercase',
            }}
          >
            thrashing · {iterations.length} loops
          </span>
        )}
      </header>

      <div style={{ display: 'flex', overflowX: 'auto' }} className="tm-scroll">
        {iterations.map((it) => (
          <IterationNode
            key={it.idx}
            iter={it}
            isSelected={selectedIdx === it.idx}
            isCurrent={it.idx === currentIdx}
            onClick={() => onSelect(it.idx)}
          />
        ))}
        {iterations.length === 0 && (
          <div
            style={{
              padding: '16px 14px',
              color: 'var(--tm-t4)',
              fontFamily: 'var(--tm-mono)',
              fontSize: 11.5,
            }}
          >
            // no iterations yet — task is queued
          </div>
        )}
      </div>
    </section>
  );
}

/* ─── Iteration detail (selected iteration's runs + steps) ─────────── */
function IterationDetail({ iter }: { iter: Iteration | null | undefined }) {
  if (!iter) return null;
  const stepCount = iter.runs.reduce((a, r) => a + r.steps.length, 0);
  return (
    <section
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
        overflow: 'hidden',
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '9px 14px',
          borderBottom: '1px solid var(--tm-border)',
          background: 'var(--tm-bg)',
        }}
      >
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 10.5,
            letterSpacing: 1.2,
            color: 'var(--tm-t3)',
            textTransform: 'uppercase',
          }}
        >
          iteration {String(iter.idx).padStart(2, '0')} · {iter.label}
        </span>
        <span style={{ color: 'var(--tm-t4)' }}>·</span>
        <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t3)' }}>
          {iter.runs.length} run{iter.runs.length !== 1 ? 's' : ''} · {stepCount} steps
        </span>
        <span style={{ flex: 1 }} />
        <StateBadge state={iter.status} size="sm" />
      </header>
      <div>
        {iter.runs.map((run) => (
          <RunBlock key={run.id} run={run} />
        ))}
      </div>
    </section>
  );
}

function RunBlock({ run }: { run: Run }) {
  const [expanded, setExpanded] = useState(
    run.status === 'running' || run.status === 'failed',
  );
  const counts = useMemo(
    () => ({
      completed: run.steps.filter((s) => s.status === 'completed').length,
      running: run.steps.filter((s) => s.status === 'running').length,
      failed: run.steps.filter((s) => s.status === 'failed').length,
      pending: run.steps.filter((s) => s.status === 'pending').length,
    }),
    [run.steps],
  );

  let dur: ReactNode;
  if (run.duration_s) {
    dur = <Metric kind="duration" value={run.duration_s} size="sm" />;
  } else if (run.started_at) {
    dur = <Age date={run.started_at} suffix="(running)" size="sm" />;
  } else {
    dur = <span style={{ color: 'var(--tm-t4)' }}>—</span>;
  }

  return (
    <div style={{ borderBottom: '1px solid var(--tm-border)' }}>
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          all: 'unset',
          cursor: 'pointer',
          width: '100%',
          display: 'grid',
          gridTemplateColumns: 'auto auto 1fr auto auto',
          gap: 12,
          alignItems: 'center',
          padding: '9px 14px',
          boxSizing: 'border-box',
        }}
      >
        <span style={{ color: 'var(--tm-t3)', display: 'flex' }}>
          {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </span>
        <WorkflowChip id={run.workflow_id} status={run.status} />
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: 'var(--tm-t3)',
          }}
        >
          <span style={{ color: 'var(--tm-t4)' }}>{run.id}</span>
          <span style={{ color: 'var(--tm-t4)' }}>·</span>
          <RunTally counts={counts} />
        </div>
        {dur}
        <StateBadge state={run.status} size="sm" />
      </button>
      {expanded && (
        <div style={{ borderTop: '1px solid var(--tm-border)', background: 'var(--tm-bg)' }}>
          {run.steps.map((step, idx) => (
            <StepRow key={step.id} step={step} idx={idx} total={run.steps.length} />
          ))}
        </div>
      )}
    </div>
  );
}

function RunTally({
  counts,
}: {
  counts: { completed: number; running: number; failed: number; pending: number };
}) {
  const items = [
    counts.completed && { count: counts.completed, label: 'done', tone: 'ok' as const },
    counts.running && { count: counts.running, label: 'running', tone: 'warn' as const },
    counts.failed && { count: counts.failed, label: 'failed', tone: 'danger' as const },
    counts.pending && { count: counts.pending, label: 'pending', tone: 'muted' as const },
  ].filter(Boolean) as { count: number; label: string; tone: 'ok' | 'warn' | 'danger' | 'muted' }[];
  return (
    <span style={{ display: 'inline-flex', gap: 10 }}>
      {items.map((it, i) => (
        <span
          key={i}
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: `var(--tm-${it.tone}-fg)`,
          }}
          className="tm-tnum"
        >
          {it.count} {it.label}
        </span>
      ))}
    </span>
  );
}

function StepStatusIcon({ status }: { status: string }) {
  if (status === 'completed') {
    return <Check size={11} strokeWidth={2.5} style={{ color: 'var(--tm-ok-fg)' }} />;
  }
  if (status === 'failed') {
    return <X size={11} strokeWidth={2.5} style={{ color: 'var(--tm-danger-fg)' }} />;
  }
  if (status === 'running') {
    return (
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: 999,
          background: 'var(--tm-warn)',
          boxShadow: '0 0 0 3px var(--tm-warn-bg)',
          animation: 'tm-pulse-soft 1.6s ease-in-out infinite',
        }}
      />
    );
  }
  return <span style={{ width: 7, height: 7, borderRadius: 999, background: 'var(--tm-t4)' }} />;
}

function StepRow({ step, idx, total }: { step: RunStep; idx: number; total: number }) {
  const [expanded, setExpanded] = useState(step.status === 'failed');
  return (
    <div
      style={{
        borderBottom: idx < total - 1 ? '1px solid var(--tm-border)' : 'none',
      }}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          all: 'unset',
          cursor: 'pointer',
          width: '100%',
          display: 'grid',
          gridTemplateColumns: 'auto auto auto 1fr auto auto auto',
          gap: 12,
          alignItems: 'center',
          padding: '7px 14px 7px 36px',
          boxSizing: 'border-box',
          fontFamily: 'var(--tm-mono)',
          fontSize: 11,
        }}
      >
        <span style={{ color: 'var(--tm-t4)', width: 16 }}>
          {String(idx + 1).padStart(2, '0')}
        </span>
        <StepStatusIcon status={step.status} />
        <span style={{ color: 'var(--tm-t1)' }}>{step.role_id}</span>
        <span
          style={{
            color: 'var(--tm-t3)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {step.output?.summary || step.error?.split('\n')[0] || ''}
        </span>
        <Metric kind="tokens" value={step.tokens.in + step.tokens.out} sub="tok" size="sm" />
        <Metric kind="duration" value={step.duration_s} size="sm" />
        <StateBadge state={step.status} size="sm" />
      </button>
      {expanded && (
        <div
          style={{
            padding: '8px 14px 12px 36px',
            background: 'var(--tm-surface)',
            borderTop: '1px solid var(--tm-border)',
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
            fontFamily: 'var(--tm-mono)',
            fontSize: 11.5,
          }}
        >
          {step.output?.summary && (
            <div>
              <Micro>output·summary</Micro>
              <div style={{ color: 'var(--tm-t1)', marginTop: 3 }}>{step.output.summary}</div>
              {step.output.commit_sha && (
                <div style={{ color: 'var(--tm-info-fg)', marginTop: 3 }}>
                  commit · {fmt.sha(step.output.commit_sha)}
                </div>
              )}
            </div>
          )}
          {step.error && (
            <div>
              <Micro style={{ color: 'var(--tm-danger-fg)' }}>error</Micro>
              <pre
                style={{
                  margin: '3px 0 0',
                  padding: 10,
                  color: 'var(--tm-danger-fg)',
                  background: 'var(--tm-danger-bg)',
                  border: '1px solid var(--tm-danger-edge)',
                  borderRadius: 2,
                  fontFamily: 'var(--tm-mono)',
                  fontSize: 11,
                  whiteSpace: 'pre-wrap',
                  lineHeight: 1.5,
                }}
              >
                {step.error}
              </pre>
            </div>
          )}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(3, auto)',
              gap: 16,
              color: 'var(--tm-t3)',
            }}
          >
            <span>
              <span style={{ color: 'var(--tm-t4)' }}>in </span>
              <Metric kind="tokens" value={step.tokens.in} size="sm" />
            </span>
            <span>
              <span style={{ color: 'var(--tm-t4)' }}>out </span>
              <Metric kind="tokens" value={step.tokens.out} size="sm" />
            </span>
            <span>
              <span style={{ color: 'var(--tm-t4)' }}>started </span>
              <Age date={step.started_at} size="sm" />
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── Blocking panel (only when blocked) ─────────────────────────── */
type BlockType = 'ci' | 'conflict' | 'review' | 'validate' | 'block';

function deriveBlockType(task: Task): BlockType | null {
  if (task.pr?.pr_conflicting) return 'conflict';
  if (task.pr?.ci_conclusion === 'failure') return 'ci';
  if (task.pr?.review_decision === 'changes_requested') return 'review';
  const tail = task.derived_status?.startsWith('blocked')
    ? task.derived_status.split('-').pop()
    : null;
  if (tail === 'ci' || tail === 'conflict' || tail === 'review' || tail === 'validate') {
    return tail;
  }
  if (task.derived_status?.startsWith('blocked')) return 'block';
  return null;
}

function BlockingPanel({ task }: { task: Task }) {
  const blockType = deriveBlockType(task);
  if (!blockType) return null;

  const TITLES: Record<BlockType, string> = {
    ci: 'CI is failing — fix before this PR can merge',
    conflict: 'Branch has merge conflicts — resolve before this PR can merge',
    review: 'Review requested changes — feedback loop must address them',
    validate: 'Validation failing — investigate before this PR can merge',
    block: 'Task is blocked',
  };
  const DETAILS: Record<
    BlockType,
    { label: string; value: string; sub: string; cmd: string | null }
  > = {
    ci: {
      label: 'failed check',
      value: 'e2e-auth',
      sub: '5/5 timing out at OAuth handoff',
      cmd: 'gh run view 4128 --log-failed | tail -50',
    },
    conflict: {
      label: 'conflicting paths',
      value: '3 files',
      sub: 'docs/adr/0050-conform-vs-adapt.md, ROADMAP.md, src/repo/mode.py',
      cmd: 'gh pr checkout 312 && git merge origin/main',
    },
    review: {
      label: 'reviewer',
      value: '@joe',
      sub: "asked for: 'split the test helper into its own module'",
      cmd: 'gh pr view 980 --comments',
    },
    validate: {
      label: 'environment',
      value: 'dev-cluster',
      sub: 'validate.smoke failed at health-check (attempt 1/3)',
      cmd: 'kubectl logs -n validate job/validate-fa9c001',
    },
    block: {
      label: 'reason',
      value: 'dependency unsatisfied',
      sub: 'blocked on tsk_8f3a2b1c (CI failing)',
      cmd: null,
    },
  };
  const d = DETAILS[blockType];
  const title = TITLES[blockType];

  return (
    <section
      style={{
        border: '1px solid var(--tm-danger-edge)',
        borderLeft: '3px solid var(--tm-danger)',
        borderRadius: 2,
        background: 'var(--tm-danger-bg)',
        overflow: 'hidden',
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '9px 14px',
          borderBottom: '1px solid var(--tm-danger-edge)',
        }}
      >
        <AlertTriangle size={13} style={{ color: 'var(--tm-danger-fg)' }} />
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            letterSpacing: 1,
            color: 'var(--tm-danger-fg)',
            fontWeight: 600,
            textTransform: 'uppercase',
          }}
        >
          blocking · {blockType}
        </span>
        <span style={{ color: 'var(--tm-t2)', fontSize: 12.5 }}>{title}</span>
        <span style={{ flex: 1 }} />
        <Age date={task.last_activity} context="blocked" suffix="stuck" size="sm" />
      </header>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '260px 1fr',
          gap: 16,
          padding: '12px 14px',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <Micro>{d.label}</Micro>
          <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 13, color: 'var(--tm-t1)' }}>
            {d.value}
          </span>
          <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t3)' }}>
            {d.sub}
          </span>
        </div>
        {d.cmd && (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 2,
              justifyContent: 'center',
            }}
          >
            <Micro>investigate</Micro>
            <code
              style={{
                fontFamily: 'var(--tm-mono)',
                fontSize: 11.5,
                color: 'var(--tm-t1)',
                padding: '5px 10px',
                background: 'var(--tm-bg)',
                border: '1px solid var(--tm-border-2)',
                borderRadius: 2,
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                alignSelf: 'flex-start',
              }}
            >
              <span style={{ color: 'var(--tm-t4)' }}>$</span>
              {d.cmd}
            </code>
          </div>
        )}
      </div>
    </section>
  );
}

/* ─── PR strip ──────────────────────────────────────────────────── */
function PRStrip({ pr }: { pr: PullRequest | null }) {
  if (!pr) return null;
  const ciTone =
    pr.ci_conclusion === 'success' ? 'ok' : pr.ci_conclusion === 'failure' ? 'danger' : 'warn';
  const revTone =
    pr.review_decision === 'approved'
      ? 'ok'
      : pr.review_decision === 'changes_requested'
        ? 'danger'
        : null;
  const valTone =
    pr.validate_decision === 'pass' ? 'ok' : pr.validate_decision === 'fail' ? 'danger' : null;
  const conflTone = pr.pr_conflicting ? 'danger' : 'ok';

  const cell = (label: string, value: ReactNode, tone?: 'ok' | 'warn' | 'danger' | null) => (
    <div
      style={{
        padding: '7px 12px',
        borderLeft: '1px solid var(--tm-border)',
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
        minWidth: 0,
      }}
    >
      <Micro>{label}</Micro>
      <span
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 12,
          color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {value}
      </span>
    </div>
  );

  const mergeBg = pr.derived_mergeability === 'mergeable'
    ? 'var(--tm-ok-bg)'
    : pr.derived_mergeability.startsWith('blocked')
      ? 'var(--tm-danger-bg)'
      : 'var(--tm-warn-bg)';

  return (
    <section
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        display: 'flex',
        alignItems: 'stretch',
        background: 'var(--tm-surface)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          padding: '7px 14px',
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
          minWidth: 124,
          background: 'var(--tm-bg)',
          borderRight: '1px solid var(--tm-border)',
        }}
      >
        <Micro>pull request</Micro>
        <a
          href="#"
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 16,
            fontWeight: 500,
            color: 'var(--tm-info-fg)',
            textDecoration: 'none',
          }}
        >
          #{pr.pr_number}
        </a>
      </div>
      {cell('branch', pr.branch)}
      {cell('head', fmt.sha(pr.head_sha))}
      {cell('ci', pr.ci_conclusion || '—', ciTone)}
      {cell(
        'review',
        pr.review_decision ? pr.review_decision.replace(/_/g, ' ') : '—',
        revTone,
      )}
      {cell('validate', pr.validate_decision || '—', valTone)}
      {cell('conflicts', pr.pr_conflicting ? 'yes' : 'clean', conflTone)}
      <div
        style={{
          marginLeft: 'auto',
          padding: '7px 14px',
          borderLeft: '1px solid var(--tm-border)',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          gap: 4,
          background: mergeBg,
          minWidth: 220,
        }}
      >
        <Micro>derived·mergeability</Micro>
        <StateBadge state={pr.derived_mergeability} size="md" />
      </div>
    </section>
  );
}

/* ─── Action bar ─────────────────────────────────────────────────── */
function ActionBar({
  task,
  onCancel,
  onAck,
}: {
  task: Task;
  onCancel: () => void;
  onAck: () => void;
}) {
  const terminal = ['done', 'merged', 'cancelled', 'validated'];
  const isNonTerminal = !terminal.includes(task.derived_status);
  const hasPR = !!task.pr;
  const lastStepFailed = task.pipeline?.some((p) => p.status === 'failed');
  const needsReviewOverride = task.pr?.review_decision === 'changes_requested';

  return (
    <div
      style={{
        display: 'flex',
        gap: 8,
        alignItems: 'center',
        flexWrap: 'wrap',
        padding: '8px 0',
      }}
    >
      {task.escalated && (
        <Button
          kind="primary"
          size="md"
          iconLeft={<Check size={12} />}
          onClick={onAck}
        >
          ack·escalation
        </Button>
      )}
      {hasPR && (
        <Button size="md" iconLeft={<ExternalLink size={12} />}>
          open·pr
        </Button>
      )}
      {lastStepFailed && (
        <Button size="md" iconLeft={<RotateCcw size={12} />}>
          retry·step
        </Button>
      )}
      {needsReviewOverride && (
        <Button size="md" iconLeft={<Check size={12} />}>
          override·review
        </Button>
      )}
      <span style={{ flex: 1 }} />
      {isNonTerminal && (
        <Button
          kind="destructive"
          size="md"
          iconLeft={<XOctagon size={12} />}
          onClick={onCancel}
        >
          cancel·task
        </Button>
      )}
    </div>
  );
}

/* ─── Cost panel (right rail) ────────────────────────────────────── */
function CostPanel({ task, iterations }: { task: Task; iterations: Iteration[] }) {
  const totalRuns = iterations.reduce((a, it) => a + it.runs.length, 0);
  const avgPerIter = iterations.length > 0 ? task.cost_usd / iterations.length : 0;
  return (
    <section
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
        overflow: 'hidden',
      }}
    >
      <header
        style={{
          padding: '9px 14px',
          borderBottom: '1px solid var(--tm-border)',
          background: 'var(--tm-bg)',
        }}
      >
        <Micro>cost·summary</Micro>
      </header>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: 1,
          background: 'var(--tm-border)',
        }}
      >
        <CostCell label="task·total" kind="usd" value={task.cost_usd} />
        <CostCell label="tokens" kind="tokens" value={task.tokens} />
        <CostCell label="iterations" kind="count" value={iterations.length} />
        <CostCell label="runs" kind="count" value={totalRuns} />
        <CostCell label="account" kind="raw" value={task.account} />
        <CostCell label="avg·per·iter" kind="usd" value={avgPerIter} />
      </div>
    </section>
  );
}

function CostCell({
  label,
  kind,
  value,
}: {
  label: string;
  kind: 'usd' | 'tokens' | 'count' | 'raw';
  value: unknown;
}) {
  return (
    <div
      style={{
        padding: '10px 14px',
        background: 'var(--tm-surface)',
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
      }}
    >
      <Micro>{label}</Micro>
      <Metric kind={kind} value={value} size="lg" />
    </div>
  );
}

/* ─── Repo arch docs panel (stub) ────────────────────────────────── */
function RepoDocsPanel({ docs, repo }: { docs: RepoDocs | null | undefined; repo: string }) {
  if (!docs) return null;
  return (
    <section
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-surface)',
        overflow: 'hidden',
      }}
    >
      <header
        style={{
          padding: '9px 14px',
          borderBottom: '1px solid var(--tm-border)',
          background: 'var(--tm-bg)',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <Micro>repo·docs</Micro>
        <span style={{ color: 'var(--tm-t4)' }}>·</span>
        <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t3)' }}>
          {repo}
        </span>
      </header>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        <DocLink href="#" label="arch.md" sub="architecture overview" />
        <DocLink href="#" label={`plans/ (${docs.plans})`} sub="active + completed plans" />
        <div
          style={{
            padding: '7px 14px',
            fontFamily: 'var(--tm-mono)',
            fontSize: 10,
            color: 'var(--tm-t4)',
            letterSpacing: 0.5,
            borderTop: '1px solid var(--tm-border)',
          }}
        >
          updated <Age date={docs.last_updated} suffix="ago" size="sm" />
          <span style={{ marginLeft: 8 }}>· markdown + mermaid (planned)</span>
        </div>
      </div>
    </section>
  );
}

function DocLink({ href, label, sub }: { href: string; label: string; sub: string }) {
  return (
    <a
      href={href}
      style={{
        padding: '9px 14px',
        borderBottom: '1px solid var(--tm-border)',
        textDecoration: 'none',
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
        transition: 'background 0.12s',
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <span
        style={{
          fontFamily: 'var(--tm-mono)',
          fontSize: 12,
          color: 'var(--tm-info-fg)',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
        }}
      >
        <Terminal size={11} />
        {label}
      </span>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>
        {sub}
      </span>
    </a>
  );
}

/* ─── Per-task events tail (right rail) ──────────────────────────── */
function EventsTailLocal({ events, taskFilter }: { events: Event[]; taskFilter: string }) {
  const items = events.filter((e) => e.task_id === taskFilter);
  return (
    <div
      className="tm-scroll"
      style={{
        flex: 1,
        overflowY: 'auto',
        minHeight: 0,
        fontFamily: 'var(--tm-mono)',
        fontSize: 11,
      }}
    >
      {items.slice(0, 20).map((e) => {
        let tone: 'muted' | 'danger' | 'ok' | 'warn' = 'muted';
        if (['failed', 'ci_failed', 'escalated_to_operator'].includes(e.action)) tone = 'danger';
        else if (['completed', 'pr_merged', 'ci_success'].includes(e.action)) tone = 'ok';
        else if (['started', 'progress', 'dispatched'].includes(e.action)) tone = 'warn';
        return (
          <div
            key={e.id}
            style={{
              display: 'grid',
              gridTemplateColumns: 'auto auto 1fr',
              gap: 8,
              padding: '5px 14px',
              borderBottom: '1px dotted var(--tm-border)',
              alignItems: 'center',
            }}
          >
            <span className="tm-tnum" style={{ color: 'var(--tm-t4)', fontSize: 10 }}>
              {fmt.time(e.created_at)}
            </span>
            <span
              style={{
                color: `var(--tm-${tone}-fg)`,
                fontSize: 9.5,
                padding: '0 4px',
                background: `var(--tm-${tone}-bg)`,
                border: `1px solid var(--tm-${tone}-edge)`,
                borderRadius: 1,
                whiteSpace: 'nowrap',
              }}
            >
              {e.entity_type}.{e.action.replace(/_/g, '·')}
            </span>
            <span
              style={{
                color: 'var(--tm-t2)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {e.detail}
            </span>
          </div>
        );
      })}
      {items.length === 0 && (
        <div
          style={{
            padding: 20,
            textAlign: 'center',
            color: 'var(--tm-t4)',
            fontSize: 11,
          }}
        >
          // no events yet for this task
        </div>
      )}
    </div>
  );
}

/* ─── Cancel modal ───────────────────────────────────────────────── */
function CancelModal({
  task,
  onClose,
  onConfirm,
}: {
  task: Task | null;
  onClose: () => void;
  onConfirm: (taskId: string, reason: string) => void;
}) {
  const [reason, setReason] = useState('');
  if (!task) return null;
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        backdropFilter: 'blur(2px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 50,
      }}
    >
      <div
        onClick={(e: MouseEvent<HTMLDivElement>) => e.stopPropagation()}
        style={{
          background: 'var(--tm-surface)',
          border: '1px solid var(--tm-border-2)',
          borderRadius: 2,
          padding: 20,
          width: 480,
          boxShadow: '0 16px 48px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
          <XOctagon size={15} style={{ color: 'var(--tm-danger-fg)' }} />
          <h3
            style={{
              margin: 0,
              fontSize: 13,
              fontWeight: 600,
              color: 'var(--tm-t1)',
              fontFamily: 'var(--tm-mono)',
              letterSpacing: 0.5,
              textTransform: 'uppercase',
            }}
          >
            cancel · task
          </h3>
        </div>
        <p
          style={{
            margin: '0 0 12px',
            color: 'var(--tm-t2)',
            fontSize: 12,
            lineHeight: 1.5,
            fontFamily: 'var(--tm-mono)',
          }}
        >
          inserts <code style={{ color: 'var(--tm-t1)' }}>task.cancelled</code> into the event
          log. in-flight workflow runs will be stopped. the PR (if any) stays open on github.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 16 }}>
          <Micro>reason (required)</Micro>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            placeholder="why are you cancelling this task?"
            style={{
              background: 'var(--tm-bg)',
              border: '1px solid var(--tm-border-2)',
              borderRadius: 2,
              padding: 10,
              color: 'var(--tm-t1)',
              fontFamily: 'var(--tm-mono)',
              fontSize: 12,
              outline: 'none',
              resize: 'vertical',
            }}
          />
        </div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <Button onClick={onClose}>keep·running</Button>
          <Button
            kind="destructive"
            disabled={!reason.trim()}
            onClick={() => {
              onConfirm(task.id, reason.trim());
              onClose();
            }}
          >
            cancel·task
          </Button>
        </div>
      </div>
    </div>
  );
}

/* ─── Top-level page ─────────────────────────────────────────────── */
export function TaskDetail() {
  const { taskId = '' } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const sim = useLiveSim();

  const detailQ = useTaskDetail(taskId);
  const docsQ = useRepoDocs(detailQ.data?.task.repo ?? '');
  const cancelM = useCancelTask();
  const ackM = useAcknowledgeEscalation();

  const iterations = useMemo(
    () => (detailQ.data ? deriveIterations(detailQ.data.runs) : []),
    [detailQ.data],
  );
  const [selectedIter, setSelectedIter] = useState<number | null>(null);
  const [cancelTarget, setCancelTarget] = useState<Task | null>(null);

  // Default selection: the latest iteration (or none if we have none yet).
  const effectiveSelected =
    selectedIter ?? (iterations.length > 0 ? iterations[iterations.length - 1].idx : 0);
  const selected = iterations.find((it) => it.idx === effectiveSelected) ?? null;

  // Same event feed as Overview; the right-rail filters it down.
  // Phase 2: serve task-scoped events from /api/dashboard/tasks/:id/events.
  const events = useMemo(() => getEvents(), [sim.tick]);

  if (detailQ.error || (!detailQ.isLoading && !detailQ.data)) {
    return (
      <PageLayout
        title="Task"
        freshness={<ConnectionAffordance mode={sim.mode} lastUpdated={sim.lastUpdated} />}
        error={
          (detailQ.error as Error) ??
          new Error(`Task not found: ${taskId}`)
        }
      >
        <div />
      </PageLayout>
    );
  }

  if (detailQ.isLoading || !detailQ.data) {
    return (
      <PageLayout
        title="Task"
        freshness={<ConnectionAffordance mode={sim.mode} lastUpdated={sim.lastUpdated} />}
        loading
      >
        <div />
      </PageLayout>
    );
  }

  const { task } = detailQ.data;
  const isBlocked =
    task.derived_status?.startsWith('blocked') ||
    task.pr?.ci_conclusion === 'failure' ||
    task.pr?.pr_conflicting;

  return (
    <PageLayout
      title={task.title}
      breadcrumb={
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 10.5,
            color: 'var(--tm-t4)',
            fontFamily: 'var(--tm-mono)',
            letterSpacing: 0.5,
          }}
        >
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              navigate('/');
            }}
            style={{ color: 'var(--tm-t3)', textDecoration: 'none' }}
          >
            tasks
          </a>
          <span>›</span>
          <span style={{ color: 'var(--tm-t2)' }}>{fmt.id(task.id)}</span>
        </div>
      }
      freshness={<ConnectionAffordance mode={sim.mode} lastUpdated={sim.lastUpdated} />}
    >
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 16 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <TaskHeader task={task} repoDocs={docsQ.data} />

          {/* Canonical lifecycle — above the fold, "where in the journey" */}
          <Lifecycle status={task.derived_status} />

          {/* HERO: iteration track — "how many times have we looped" */}
          <IterationTrack
            iterations={iterations}
            selectedIdx={effectiveSelected}
            onSelect={setSelectedIter}
          />

          {/* Blocking — promoted above the PR strip when blocked */}
          {isBlocked && <BlockingPanel task={task} />}

          <PRStrip pr={task.pr} />

          <ActionBar
            task={task}
            onCancel={() => setCancelTarget(task)}
            onAck={() => ackM.mutate({ taskId: task.id })}
          />

          <IterationDetail iter={selected} />
        </div>

        {/* Right rail */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <CostPanel task={task} iterations={iterations} />
          <RepoDocsPanel docs={docsQ.data} repo={task.repo} />

          <section
            style={{
              border: '1px solid var(--tm-border)',
              borderRadius: 2,
              background: 'var(--tm-surface)',
              overflow: 'hidden',
              display: 'flex',
              flexDirection: 'column',
              flex: 1,
              minHeight: 280,
            }}
          >
            <header
              style={{
                padding: '9px 14px',
                borderBottom: '1px solid var(--tm-border)',
                background: 'var(--tm-bg)',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
              }}
            >
              <Micro>events.task</Micro>
              <span style={{ flex: 1 }} />
              <span
                style={{
                  fontFamily: 'var(--tm-mono)',
                  fontSize: 10,
                  color: 'var(--tm-t4)',
                }}
              >
                filtered to {fmt.id(task.id, 8)}…
              </span>
            </header>
            <EventsTailLocal events={events} taskFilter={task.id} />
          </section>
        </div>
      </div>

      <CancelModal
        task={cancelTarget}
        onClose={() => setCancelTarget(null)}
        onConfirm={(id, reason) => cancelM.mutate({ taskId: id, reason })}
      />
    </PageLayout>
  );
}
