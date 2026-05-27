/**
 * Overview — Treadmill operator dashboard `/`.
 *
 * Ported from the Claude Design handoff bundle (treadmill-overview-v2.jsx,
 * direction C). Organized around the three operator questions:
 * Blocked (needs you), In-flight (agents working), Hopper (queued).
 * Blocked first by design.
 */

import { type ReactNode, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Activity, AlertTriangle, Box, CheckCircle2 } from 'lucide-react';

import { Age, Caret, Metric } from '../design/Metric';
import { Button } from '../design/Button';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { AccountPill, RepoCell } from '../design/chrome';
import { StateBadge } from '../design/StateBadge';
import { PageLayout } from '../design/PageLayout';
import { fmt, tones, type Tone } from '../design/fmt';

import {
  useAcknowledgeEscalation,
  useOverview,
  type OverviewFilters,
} from '../api/queries';
import { useLiveSim } from '../api/sim';
import { getTasks, operatorBucket } from '../api/mock';
import type { Account, Bucket, Escalation, Event, Fleet, Task } from '../api/types';

/* ─── Section banner ────────────────────────────────────────────── */
function SectionBanner({
  label,
  count,
  sub,
  tone = 'muted',
  icon,
}: {
  label: string;
  count: number;
  sub?: string;
  tone?: Tone;
  icon?: ReactNode;
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '10px 0 6px',
        fontFamily: 'var(--tm-mono)',
      }}
    >
      <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, paddingRight: 8 }}>
        {icon && (
          <span style={{ color: `var(--tm-${tone}-fg)`, display: 'inline-flex' }}>{icon}</span>
        )}
        <span
          style={{
            fontSize: 11,
            letterSpacing: 1.6,
            fontWeight: 600,
            color: `var(--tm-${tone}-fg)`,
            textTransform: 'uppercase',
          }}
        >
          {label}
        </span>
        <span
          className="tm-tnum"
          style={{
            fontSize: 12,
            color: `var(--tm-${tone}-fg)`,
            padding: '1px 7px',
            background: `var(--tm-${tone}-bg)`,
            border: `1px solid var(--tm-${tone}-edge)`,
            borderRadius: 2,
          }}
        >
          {count}
        </span>
      </div>
      <div style={{ flex: 1, borderTop: '1px dashed var(--tm-border)', height: 0 }} />
      {sub && (
        <span
          style={{
            fontSize: 10.5,
            color: 'var(--tm-t4)',
            letterSpacing: 0.6,
            textTransform: 'lowercase',
          }}
        >
          {sub}
        </span>
      )}
    </div>
  );
}

/* ─── Bucket header — three clickable counters ──────────────────── */
function BucketHeader({
  counts,
  focus,
  onFocusChange,
}: {
  counts: { blocked: number; inflight: number; hopper: number; total: number };
  focus: Bucket | null;
  onFocusChange: (b: Bucket | null) => void;
}) {
  const cells: { key: Bucket; label: string; sub: string; tone: Tone; value: number }[] = [
    { key: 'blocked', label: 'Blocked', sub: 'needs you', tone: 'danger', value: counts.blocked },
    {
      key: 'inflight',
      label: 'In-flight',
      sub: 'agents working',
      tone: 'warn',
      value: counts.inflight,
    },
    { key: 'hopper', label: 'Hopper', sub: 'queued', tone: 'muted', value: counts.hopper },
  ];
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr 1fr',
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        overflow: 'hidden',
        background: 'var(--tm-surface)',
      }}
    >
      {cells.map((c, i) => {
        const isActive = focus === c.key;
        return (
          <button
            key={c.key}
            onClick={() => onFocusChange(isActive ? null : c.key)}
            style={{
              padding: '14px 16px',
              borderTop: isActive
                ? `2px solid var(--tm-${c.tone})`
                : '2px solid transparent',
              background: isActive ? `var(--tm-${c.tone}-bg)` : 'transparent',
              borderLeft: 'none',
              borderRight: i < cells.length - 1 ? '1px solid var(--tm-border)' : 'none',
              borderBottom: 'none',
              cursor: 'pointer',
              textAlign: 'left',
              fontFamily: 'var(--tm-mono)',
              color: 'var(--tm-t1)',
              transition: 'background 0.12s',
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
              outline: 'none',
            }}
            onMouseEnter={(e) => {
              if (!isActive) e.currentTarget.style.background = 'var(--tm-hover)';
            }}
            onMouseLeave={(e) => {
              if (!isActive) e.currentTarget.style.background = 'transparent';
            }}
          >
            <span
              style={{
                fontSize: 10.5,
                letterSpacing: 1,
                textTransform: 'uppercase',
                color: `var(--tm-${c.tone}-fg)`,
                fontWeight: 500,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <span
                style={{ width: 6, height: 6, borderRadius: 999, background: `var(--tm-${c.tone})` }}
              />
              {c.label}
            </span>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <Metric kind="count" value={c.value} size="xl" tone={c.tone} />
              <span style={{ color: 'var(--tm-t4)', fontSize: 11 }}>{c.sub}</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}

/* ─── Spend strip + Fleet row ───────────────────────────────────── */
function ColLabel({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        width: 96,
        padding: '8px 14px',
        fontFamily: 'var(--tm-mono)',
        fontSize: 10,
        color: 'var(--tm-t4)',
        letterSpacing: 1.2,
        textTransform: 'uppercase',
        display: 'flex',
        alignItems: 'center',
        background: 'var(--tm-bg)',
        borderRight: '1px solid var(--tm-border)',
      }}
    >
      {children}
    </div>
  );
}

function ColMicro({ children }: { children: ReactNode }) {
  return (
    <span
      style={{
        fontFamily: 'var(--tm-mono)',
        fontSize: 9.5,
        color: 'var(--tm-t4)',
        letterSpacing: 0.8,
        textTransform: 'uppercase',
        display: 'inline-flex',
        alignItems: 'center',
      }}
    >
      {children}
    </span>
  );
}

function FleetCellC({
  label,
  children,
}: {
  label: string;
  tone?: Tone;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        flex: 1,
        padding: '8px 14px',
        borderLeft: '1px solid var(--tm-border)',
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
        minWidth: 0,
      }}
    >
      <ColMicro>{label}</ColMicro>
      <div>{children}</div>
    </div>
  );
}

function TopStripC({ accounts, fleet }: { accounts: Account[]; fleet: Fleet }) {
  const total24h = accounts.reduce((a, b) => a + b.usd_est_24h, 0);
  const totalTokens = accounts.reduce((a, b) => a + b.tokens_24h, 0);
  const schedAge = Math.floor((Date.now() - new Date(fleet.scheduler_last_tick).getTime()) / 1000);
  const autoAge = Math.floor((Date.now() - new Date(fleet.autoscaler_last_tick).getTime()) / 1000);

  return (
    <div
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        overflow: 'hidden',
        background: 'var(--tm-surface)',
      }}
    >
      {/* spend.24h */}
      <div style={{ display: 'flex', borderBottom: '1px solid var(--tm-border)' }}>
        <ColLabel>spend.24h</ColLabel>
        <div
          style={{
            padding: '8px 14px',
            borderLeft: '1px solid var(--tm-border)',
            display: 'flex',
            flexDirection: 'column',
            gap: 1,
            minWidth: 130,
          }}
        >
          <ColMicro>total</ColMicro>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <Metric kind="usd" value={total24h} size="lg" />
            <Metric kind="tokens" value={totalTokens} size="sm" sub="tok" />
          </div>
        </div>
        {accounts.map((a) => (
          <div
            key={a.name}
            style={{
              flex: 1,
              padding: '8px 14px',
              borderLeft: '1px solid var(--tm-border)',
              display: 'flex',
              flexDirection: 'column',
              gap: 1,
              minWidth: 0,
            }}
          >
            <ColMicro>
              <span
                style={{
                  display: 'inline-block',
                  width: 5,
                  height: 5,
                  borderRadius: 999,
                  background: 'var(--tm-t3)',
                  marginRight: 6,
                }}
              />
              {a.name}
            </ColMicro>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <Metric kind="usd" value={a.usd_est_24h} size="md" />
              <Metric kind="tokens" value={a.tokens_24h} size="sm" sub="tok" />
            </div>
          </div>
        ))}
      </div>

      {/* fleet */}
      <div style={{ display: 'flex' }}>
        <ColLabel>fleet</ColLabel>
        <FleetCellC label="workers" tone="warn">
          <Metric kind="raw" value={`${fleet.workers_running}/${fleet.workers_capacity}`} size="md" />
        </FleetCellC>
        <FleetCellC label="sched.tick" tone={tones.heartbeat(schedAge)}>
          <Metric
            kind="duration"
            value={schedAge}
            sub="ago"
            size="md"
            tone={tones.heartbeat(schedAge)}
          />
        </FleetCellC>
        <FleetCellC label="autoscaler" tone={tones.heartbeat(autoAge)}>
          <Metric
            kind="duration"
            value={autoAge}
            sub="ago"
            size="md"
            tone={tones.heartbeat(autoAge)}
          />
        </FleetCellC>
        <FleetCellC label="alive">
          <Metric kind="age" value={fleet.scheduler_alive_since} size="md" />
        </FleetCellC>
      </div>
    </div>
  );
}

/* ─── Task row (compact, Metric-driven, no per-section table header dup) */
function TaskRowHeader() {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '180px 1fr 200px 80px 110px 84px',
        gap: 12,
        padding: '6px 14px',
        borderBottom: '1px solid var(--tm-border)',
        background: 'var(--tm-bg)',
        fontFamily: 'var(--tm-mono)',
        fontSize: 9.5,
        letterSpacing: 1,
        color: 'var(--tm-t4)',
        textTransform: 'uppercase',
      }}
    >
      <span>repo</span>
      <span>task</span>
      <span>stage</span>
      <span>last·activity</span>
      <span>pr</span>
      <span style={{ textAlign: 'right' }}>acct</span>
    </div>
  );
}

function TaskRowC({
  task,
  onClick,
  flash,
  bucket,
}: {
  task: Task;
  onClick: () => void;
  flash: boolean;
  bucket: Bucket;
}) {
  const stageContext =
    bucket === 'blocked' ? 'blocked' : bucket === 'inflight' ? 'in-flight' : null;
  return (
    <button
      onClick={onClick}
      style={{
        width: '100%',
        display: 'grid',
        gridTemplateColumns: '180px 1fr 200px 80px 110px 84px',
        gap: 12,
        alignItems: 'center',
        padding: '8px 14px',
        background: 'transparent',
        border: 'none',
        borderBottom: '1px solid var(--tm-border)',
        cursor: 'pointer',
        textAlign: 'left',
        color: 'var(--tm-t1)',
        transition: 'background 0.12s',
        animation: flash ? 'tm-flash-row 1.4s ease-out 1' : 'none',
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <RepoCell repo={task.repo} mode={task.repo_mode} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
        <span
          style={{
            color: 'var(--tm-t1)',
            fontSize: 12.5,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {task.title}
        </span>
        <span
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 10.5,
            color: 'var(--tm-t4)',
            display: 'flex',
            gap: 8,
          }}
        >
          <span style={{ color: 'var(--tm-t3)' }}>{fmt.id(task.id)}</span>
          <span>·</span>
          <span style={{ color: 'var(--tm-t3)' }}>{task.plan_id}</span>
        </span>
      </div>
      <div>
        <StateBadge state={task.derived_status} size="sm" />
      </div>
      <Age date={task.last_activity} context={stageContext} suffix="" size="md" />
      <div style={{ minWidth: 0 }}>
        {task.pr ? (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span
              style={{
                fontFamily: 'var(--tm-mono)',
                fontSize: 11.5,
                color: 'var(--tm-info-fg)',
                textDecoration: 'none',
              }}
            >
              #{task.pr.pr_number}
            </span>
            <span
              style={{
                width: 5,
                height: 5,
                borderRadius: 999,
                background:
                  task.pr.derived_mergeability === 'mergeable'
                    ? 'var(--tm-ok)'
                    : task.pr.derived_mergeability.startsWith('blocked')
                      ? 'var(--tm-danger)'
                      : 'var(--tm-warn)',
              }}
            />
          </span>
        ) : (
          <span style={{ color: 'var(--tm-t4)' }}>—</span>
        )}
      </div>
      <div style={{ textAlign: 'right' }}>
        <AccountPill name={task.account} />
      </div>
    </button>
  );
}

/* ─── Bucket section ─────────────────────────────────────────────── */
function BucketSection({
  bucket,
  label,
  tone,
  sub,
  icon,
  tasks,
  flashIds,
  onSelectTask,
}: {
  bucket: Bucket;
  label: string;
  tone: Tone;
  sub?: string;
  icon?: ReactNode;
  tasks: Task[];
  flashIds: Set<string>;
  onSelectTask: (taskId: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const emptyMsg =
    bucket === 'blocked'
      ? "// no blockers. you're free."
      : bucket === 'inflight'
        ? '// nothing running.'
        : '// hopper empty.';
  return (
    <section>
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          all: 'unset',
          cursor: 'pointer',
          display: 'block',
          width: '100%',
        }}
      >
        <SectionBanner label={label} count={tasks.length} sub={sub} tone={tone} icon={icon} />
      </button>
      {expanded && (
        <div
          style={{
            border: '1px solid var(--tm-border)',
            borderLeft: `2px solid var(--tm-${tone}-edge)`,
            borderRadius: 2,
            overflow: 'hidden',
            background: 'var(--tm-surface)',
          }}
        >
          <TaskRowHeader />
          {tasks.length === 0 ? (
            <div
              style={{
                padding: '16px 14px',
                textAlign: 'center',
                color: 'var(--tm-t4)',
                fontFamily: 'var(--tm-mono)',
                fontSize: 11.5,
              }}
            >
              {emptyMsg}
            </div>
          ) : (
            tasks.map((t) => (
              <TaskRowC
                key={t.id}
                task={t}
                onClick={() => onSelectTask(t.id)}
                flash={flashIds.has(t.id)}
                bucket={bucket}
              />
            ))
          )}
        </div>
      )}
    </section>
  );
}

/* ─── Escalation strip ──────────────────────────────────────────── */
function EscalationStripC({
  items,
  onAck,
}: {
  items: Escalation[];
  onAck: (taskId: string) => void;
}) {
  if (!items.length) return null;
  return (
    <div
      style={{
        border: '1px solid var(--tm-danger-edge)',
        borderLeft: '3px solid var(--tm-danger)',
        borderRadius: 2,
        background: 'var(--tm-danger-bg)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '7px 14px',
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
          {items.length} health-bot escalation{items.length !== 1 ? 's' : ''}
        </span>
        <span style={{ color: 'var(--tm-t4)', fontSize: 11, fontFamily: 'var(--tm-mono)' }}>
          · unacknowledged
        </span>
      </div>
      {items.map((it) => (
        <div
          key={it.task_id}
          style={{
            display: 'grid',
            gridTemplateColumns: 'auto 1fr auto auto',
            alignItems: 'center',
            gap: 14,
            padding: '7px 14px',
            borderBottom: '1px solid var(--tm-danger-edge)',
            fontFamily: 'var(--tm-mono)',
            fontSize: 11.5,
          }}
        >
          <span style={{ color: 'var(--tm-t3)' }}>{it.repo}</span>
          <span style={{ color: 'var(--tm-t1)' }}>
            {it.title}
            <span style={{ color: 'var(--tm-danger-fg)', marginLeft: 10 }}>— {it.reason}</span>
          </span>
          <Age date={it.escalated_at} context="blocked" suffix="stuck" size="sm" />
          <Button
            size="sm"
            iconLeft={<CheckCircle2 size={11} />}
            onClick={() => onAck(it.task_id)}
          >
            ack
          </Button>
        </div>
      ))}
    </div>
  );
}

/* ─── Filter row ─────────────────────────────────────────────────── */
function FilterRow({
  repoFilter,
  setRepoFilter,
  accountFilter,
  setAccountFilter,
  q,
  setQ,
  count,
  total,
  accounts,
}: {
  repoFilter: string | null;
  setRepoFilter: (v: string | null) => void;
  accountFilter: string | null;
  setAccountFilter: (v: string | null) => void;
  q: string;
  setQ: (v: string) => void;
  count: number;
  total: number;
  accounts: Account[];
}) {
  const repoOptions = useMemo(() => [...new Set(getTasks().map((t) => t.repo))], []);
  return (
    <div
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        padding: '6px 12px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        flexWrap: 'wrap',
        fontFamily: 'var(--tm-mono)',
        fontSize: 11,
        color: 'var(--tm-t3)',
        background: 'var(--tm-surface)',
      }}
    >
      <span style={{ color: 'var(--tm-t4)' }}>where</span>
      <MiniSelect label="repo" value={repoFilter} onChange={setRepoFilter} options={repoOptions} />
      <MiniSelect
        label="account"
        value={accountFilter}
        onChange={setAccountFilter}
        options={accounts.map((a) => a.name)}
      />
      <MiniSearch value={q} onChange={setQ} />
      <span style={{ flex: 1 }} />
      <span style={{ color: 'var(--tm-t4)' }}>showing</span>
      <span className="tm-tnum" style={{ color: 'var(--tm-t1)' }}>
        {count}
      </span>
      <span style={{ color: 'var(--tm-t4)' }}>of</span>
      <span className="tm-tnum" style={{ color: 'var(--tm-t2)' }}>
        {total}
      </span>
    </div>
  );
}

function MiniSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string | null;
  onChange: (v: string | null) => void;
  options: string[];
}) {
  return (
    <label
      style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: 'var(--tm-t3)' }}
    >
      <span style={{ color: 'var(--tm-t4)' }}>{label}</span>
      <span style={{ color: 'var(--tm-t4)' }}>=</span>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        style={{
          background: 'transparent',
          border: 'none',
          color: value ? 'var(--tm-warn-fg)' : 'var(--tm-t1)',
          fontFamily: 'inherit',
          fontSize: 'inherit',
          outline: 'none',
          cursor: 'pointer',
          appearance: 'none',
          paddingRight: 12,
        }}
      >
        <option value="">*</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

function MiniSearch({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        color: 'var(--tm-t3)',
        padding: '2px 6px',
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        background: 'var(--tm-bg)',
      }}
    >
      <span style={{ color: 'var(--tm-t4)' }}>q:</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="…"
        style={{
          background: 'transparent',
          border: 'none',
          color: 'var(--tm-t1)',
          fontFamily: 'inherit',
          fontSize: 'inherit',
          outline: 'none',
          width: 110,
        }}
      />
    </label>
  );
}

/* ─── Events tail ───────────────────────────────────────────────── */
function EventsTail({ events }: { events: Event[] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const items = events;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div
        ref={ref}
        className="tm-scroll"
        style={{
          overflowY: 'auto',
          flex: 1,
          minHeight: 0,
          fontFamily: 'var(--tm-mono)',
          fontSize: 11,
        }}
      >
        {items.slice(0, 30).map((e) => {
          let tone: Tone = 'muted';
          if (['failed', 'ci_failed', 'escalated_to_operator'].includes(e.action)) tone = 'danger';
          else if (['completed', 'pr_merged', 'ci_success'].includes(e.action)) tone = 'ok';
          else if (['started', 'progress', 'dispatched', 'tick'].includes(e.action)) tone = 'warn';
          return (
            <div
              key={e.id}
              style={{
                display: 'grid',
                gridTemplateColumns: 'auto auto 1fr',
                gap: 8,
                padding: '3px 14px',
                borderBottom: '1px dotted var(--tm-border)',
                alignItems: 'center',
                lineHeight: 1.45,
              }}
            >
              <span className="tm-tnum" style={{ color: 'var(--tm-t4)', fontSize: 10.5 }}>
                {fmt.time(e.created_at)}
              </span>
              <span
                style={{
                  color: `var(--tm-${tone}-fg)`,
                  fontSize: 10,
                  padding: '0 4px',
                  background: `var(--tm-${tone}-bg)`,
                  border: `1px solid var(--tm-${tone}-edge)`,
                  borderRadius: 1,
                  letterSpacing: 0.3,
                  minWidth: 88,
                  textAlign: 'center',
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
                {e.task_id && (
                  <span style={{ color: 'var(--tm-info-fg)', marginRight: 6 }}>
                    {fmt.id(e.task_id, 12)}
                  </span>
                )}
                {e.detail}
              </span>
            </div>
          );
        })}
      </div>
      <div
        style={{
          padding: '6px 14px',
          borderTop: '1px solid var(--tm-border)',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          fontFamily: 'var(--tm-mono)',
          fontSize: 10.5,
          background: 'var(--tm-bg)',
        }}
      >
        <span style={{ color: 'var(--tm-t4)' }}>$</span>
        <span style={{ color: 'var(--tm-t2)' }}>tail -f events</span>
        <span style={{ color: 'var(--tm-t4)' }}>—</span>
        <span style={{ color: 'var(--tm-t3)' }}>{items.length} shown</span>
        <span style={{ flex: 1 }} />
        <button
          onClick={() => setAutoScroll(!autoScroll)}
          style={{
            all: 'unset',
            cursor: 'pointer',
            fontSize: 10,
            color: autoScroll ? 'var(--tm-warn-fg)' : 'var(--tm-t4)',
            letterSpacing: 0.6,
          }}
        >
          {autoScroll ? '● tailing' : '○ paused'}
        </button>
        <Caret />
      </div>
    </div>
  );
}

/* ─── Overview — top-level ──────────────────────────────────────── */
export function Overview() {
  const navigate = useNavigate();
  const sim = useLiveSim();
  const [repoFilter, setRepoFilter] = useState<string | null>(null);
  const [accountFilter, setAccountFilter] = useState<string | null>(null);
  const [q, setQ] = useState('');
  const [focus, setFocus] = useState<Bucket | null>(null);

  const filters: OverviewFilters = {
    repo: repoFilter ?? undefined,
    account: accountFilter ?? undefined,
    q: q || undefined,
  };
  const { data, isLoading, error } = useOverview(filters);
  const ack = useAcknowledgeEscalation();

  const grouped = useMemo(() => {
    const all = data?.tasks ?? [];
    return {
      blocked: all.filter((t: Task) => operatorBucket(t) === 'blocked'),
      inflight: all.filter((t: Task) => operatorBucket(t) === 'inflight'),
      hopper: all.filter((t: Task) => operatorBucket(t) === 'hopper'),
    };
  }, [data?.tasks]);

  const buckets: {
    key: Bucket;
    label: string;
    tone: Tone;
    sub: string;
    icon: ReactNode;
  }[] = [
    {
      key: 'blocked',
      label: 'Blocked',
      tone: 'danger',
      sub: 'needs your attention',
      icon: <AlertTriangle size={13} />,
    },
    {
      key: 'inflight',
      label: 'In-flight',
      tone: 'warn',
      sub: 'agents are working',
      icon: <Activity size={13} />,
    },
    {
      key: 'hopper',
      label: 'Hopper',
      tone: 'muted',
      sub: 'queued · not started',
      icon: <Box size={13} />,
    },
  ];
  const visibleBuckets = focus ? buckets.filter((b) => b.key === focus) : buckets;

  return (
    <PageLayout
      title="overview"
      loading={isLoading}
      error={error as Error | null}
      breadcrumb={
        <span
          style={{
            fontSize: 10.5,
            color: 'var(--tm-t4)',
            fontFamily: 'var(--tm-mono)',
            letterSpacing: 0.8,
            textTransform: 'uppercase',
          }}
        >
          treadmill · operator
        </span>
      }
      freshness={<ConnectionAffordance mode={sim.mode} lastUpdated={sim.lastUpdated} />}
      actions={
        <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t3)' }}>
          <span style={{ color: 'var(--tm-t4)' }}>$</span> treadmill tasks --live
        </span>
      }
    >
      {data && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 16, minHeight: 760 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <EscalationStripC
              items={data.escalations}
              onAck={(id) => ack.mutate({ taskId: id })}
            />
            <TopStripC accounts={data.accounts} fleet={data.fleet} />
            <BucketHeader counts={data.bucketCounts} focus={focus} onFocusChange={setFocus} />
            <FilterRow
              repoFilter={repoFilter}
              setRepoFilter={setRepoFilter}
              accountFilter={accountFilter}
              setAccountFilter={setAccountFilter}
              q={q}
              setQ={setQ}
              count={data.tasks.length}
              total={data.bucketCounts.total}
              accounts={data.accounts}
            />
            {visibleBuckets.map((b) => (
              <BucketSection
                key={b.key}
                bucket={b.key}
                label={b.label}
                tone={b.tone}
                sub={b.sub}
                icon={b.icon}
                tasks={grouped[b.key]}
                flashIds={sim.flashIds}
                onSelectTask={(id) => navigate(`/tasks/${id}`)}
              />
            ))}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, minHeight: 0 }}>
            <div
              style={{
                border: '1px solid var(--tm-border)',
                borderRadius: 2,
                background: 'var(--tm-surface)',
                display: 'flex',
                flexDirection: 'column',
                flex: 1,
                minHeight: 0,
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
                    fontWeight: 500,
                  }}
                >
                  events.live
                </span>
                <span style={{ flex: 1 }} />
                <span
                  style={{
                    fontSize: 9.5,
                    fontFamily: 'var(--tm-mono)',
                    color: 'var(--tm-warn-fg)',
                    letterSpacing: 1,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 5,
                  }}
                >
                  <span
                    style={{
                      width: 5,
                      height: 5,
                      borderRadius: 999,
                      background: 'var(--tm-warn)',
                      animation: 'tm-pulse-soft 1.6s ease-in-out infinite',
                    }}
                  />
                  STREAMING
                </span>
              </header>
              <EventsTail events={data.events} />
            </div>
          </div>
        </div>
      )}
    </PageLayout>
  );
}
