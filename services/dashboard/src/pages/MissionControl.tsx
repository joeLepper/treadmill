/**
 * Mission Control (S1 + S5) — the landing page.
 *
 * Per Alan's Q1 vote: "is it alive" (team roster) + "what is it doing"
 * (loop activity feed) are one glance. Roster is the hero; the feed is a
 * right rail. Replaces v1's fleet/autoscaler panel (subject deleted in
 * ADR-0087) with the per-repo team that actually runs the work.
 */

import { useNavigate } from 'react-router-dom';
import {
  Activity,
  ChevronRight,
  CircleDot,
  GitMerge,
  Radio,
  ScrollText,
  ShieldCheck,
  Users,
} from 'lucide-react';
import { PageLayout } from '../design/PageLayout';
import { Panel } from '../design/Panel';
import { ConnectionAffordance } from '../design/ConnectionAffordance';
import { StepTranscriptDrawer } from '../design/StepTranscript';
import { WorkerDrawer } from '../design/WorkerDrawer';
import { stepTranscript } from '../api/stepTranscript';
import { useViewState } from '../design/useViewState';
import { useOverview, useTeamConfigs, useTaskJourney, feedFromEvents, parseStatus, type OverviewTask } from '../api/v2queries';
import { fmt } from '../design/fmt';
import type { Tone } from '../design/fmt';
import {
  type FeedEvent,
  type FeedKind,
  type Session,
  type Team,
} from '../api/v2mock';

const STATE_TONE: Record<Session['state'], Tone> = {
  live: 'ok',
  idle: 'muted',
  down: 'danger',
};

const FEED_TONE: Record<FeedKind, Tone> = {
  dispatch: 'info',
  ci: 'warn',
  review: 'info',
  verdict: 'ok',
  merge: 'ok',
  escalation: 'danger',
  deploy: 'muted',
  digest: 'muted',
};

const FEED_ICON: Record<FeedKind, typeof Activity> = {
  dispatch: Radio,
  ci: CircleDot,
  review: Users,
  verdict: ShieldCheck,
  merge: GitMerge,
  escalation: Activity,
  deploy: ScrollText,
  digest: ScrollText,
};

function ageS(iso: string | null): number {
  return iso ? Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000)) : 0;
}

/** Real roster from /team_configs, with each worker's current work resolved
 *  from the overview's non-terminal tasks. Liveness is approximated (no
 *  per-session heartbeat endpoint): a worker with in-flight work is live,
 *  coordinator/evaluator are always-on. today-counts aren't served → 0. */
function buildLiveTeams(cfgs: { repo: string; coordinator_label: string; evaluator_label: string; worker_labels: string[] }[], ovTasks: OverviewTask[]): Team[] {
  const sess = (label: string, role: Session['role'], current: Session['current']): Session => ({
    label, role,
    state: current.length ? 'live' : role === 'worker' ? 'idle' : 'live',
    lastEventAgeS: current.length ? ageS(ovTasks.find((t) => t.id === current[0].taskId)?.last_activity ?? null) : 0,
    current,
    today: { initial: 0, rework: 0, review: 0 },
  });
  const currentFor = (label: string) =>
    ovTasks
      .filter((t) => parseStatus(t.derived_status).worker === label)
      .map((t) => ({ taskId: t.id, title: t.title, trigger: 'initial' as const, startedAgeS: ageS(t.started_at ?? t.created_at) }));
  return cfgs.map((c) => ({
    repo: c.repo,
    slug: c.repo.replace('/', '-').toLowerCase(),
    coordinator: sess(c.coordinator_label, 'coordinator', []),
    evaluator: sess(c.evaluator_label, 'evaluator', []),
    workers: c.worker_labels.map((w) => sess(w, 'worker', currentFor(w))),
  }));
}

export function MissionControl() {
  const v = useViewState();
  const sessionLabel = v.get('session');
  const step = stepTranscript(v.get('step'));

  // Live feed + roster from the overview aggregate + /team_configs. No mock.
  const overview = useOverview();
  const teamCfg = useTeamConfigs();
  const loading = overview.isLoading || teamCfg.isLoading;
  const error = overview.error ?? teamCfg.error;
  const renderTeams = overview.data && teamCfg.data ? buildLiveTeams(teamCfg.data.teams, overview.data.tasks) : [];
  const feedEvents = overview.data ? feedFromEvents(overview.data.events) : [];
  const allSessions = renderTeams.flatMap((t) => [t.coordinator, t.evaluator, ...t.workers]);
  const session = sessionLabel ? allSessions.find((s) => s.label === sessionLabel) : undefined;
  const openTaskId = session?.current.find((c) => !c.awaitingMerge)?.taskId;
  const drawerJourney = useTaskJourney(openTaskId);
  const liveContrib = drawerJourney.data;

  return (
    <PageLayout
      title="mission control"
      freshness={<ConnectionAffordance mode="ws" lastUpdated={new Date().toISOString()} />}
      actions={renderTeams.length ? <FleetSummary teams={renderTeams} live /> : undefined}
      loading={loading}
      error={error instanceof Error ? error : error ? new Error(String(error)) : null}
    >
      {/* drawers — URL-addressable; the step drawer layers over the worker drawer */}
      {session && (
        <WorkerDrawer
          session={session}
          liveCycles={liveContrib}
          onClose={() => v.set('session', null)}
          onOpenStep={(id) => v.set('step', id)}
        />
      )}
      {step && <StepTranscriptDrawer step={step} onClose={() => v.set('step', null)} />}

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0,1fr) 348px',
          gap: 16,
          alignItems: 'start',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {renderTeams.map((t) => (
            <TeamCard key={t.slug} team={t} v={v} />
          ))}
        </div>
        <FeedRail events={feedEvents} />
      </div>
    </PageLayout>
  );
}

function FleetSummary({ teams: tms, live }: { teams: Team[]; live: boolean }) {
  const sessions = tms.flatMap((t) => [t.coordinator, t.evaluator, ...t.workers]);
  const liveCount = sessions.filter((s) => s.state === 'live').length;
  const running = sessions.reduce((n, s) => n + s.current.filter((c) => !c.awaitingMerge).length, 0);
  return (
    <div style={{ display: 'flex', gap: 18, alignItems: 'center', fontFamily: 'var(--tm-mono)' }}>
      <span title={live ? 'live · /team_configs (liveness approximated)' : 'mock'} style={{ fontSize: 8.5, letterSpacing: 0.4, textTransform: 'uppercase', color: live ? 'var(--tm-ok-fg)' : 'var(--tm-t4)', border: `1px solid ${live ? 'var(--tm-ok-edge)' : 'var(--tm-border)'}`, borderRadius: 3, padding: '1px 5px' }}>{live ? 'live' : 'mock'}</span>
      <SummaryStat label="teams" value={String(tms.length)} />
      <SummaryStat label="sessions live" value={`${liveCount}/${sessions.length}`} tone="ok" />
      <SummaryStat label="executing" value={String(running)} tone="warn" />
    </div>
  );
}

function SummaryStat({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
      <span
        className="tm-tnum"
        style={{ fontSize: 16, fontWeight: 500, color: tone ? `var(--tm-${tone}-fg)` : 'var(--tm-t1)' }}
      >
        {value}
      </span>
      <span style={{ fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>
        {label}
      </span>
    </div>
  );
}

function TeamCard({ team, v }: { team: Team; v: ReturnType<typeof useViewState> }) {
  const navigate = useNavigate();
  return (
    <Panel
      title={
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <Users size={12} style={{ opacity: 0.7 }} />
          {team.repo}
        </span>
      }
      actions={
        <button
          onClick={() => navigate('/tasks')}
          style={{
            background: 'transparent',
            border: '1px solid var(--tm-border-2)',
            color: 'var(--tm-t3)',
            fontFamily: 'var(--tm-mono)',
            fontSize: 10.5,
            padding: '3px 9px',
            borderRadius: 2,
            cursor: 'pointer',
            letterSpacing: 0.3,
          }}
        >
          loop ↗
        </button>
      }
      padded={false}
    >
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(232px, 1fr))', gap: 1, background: 'var(--tm-border)' }}>
        <SessionTile s={team.coordinator} v={v} />
        <SessionTile s={team.evaluator} v={v} />
        {team.workers.map((w) => (
          <SessionTile key={w.label} s={w} v={v} />
        ))}
      </div>
    </Panel>
  );
}

function SessionTile({ s, v }: { s: Session; v: ReturnType<typeof useViewState> }) {
  const tone = STATE_TONE[s.state];
  // Strip the team-slug middle of a session label for compact display:
  // role-<owner>-<name>[-N] → role[-N]. Generic — no hardcoded team names.
  const shortLabel = s.label.replace(/^(coordinator|evaluator|worker)-.+?(-\d+)?$/, '$1$2');
  const liveExec = s.current.filter((c) => !c.awaitingMerge);
  const awaiting = s.current.filter((c) => c.awaitingMerge);
  return (
    <div
      onClick={() => v.set('session', s.label)}
      style={{ background: 'var(--tm-bg)', padding: '11px 13px', display: 'flex', flexDirection: 'column', gap: 9, minHeight: 96, cursor: 'pointer' }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--tm-bg)')}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: 999,
            background: `var(--tm-${tone})`,
            animation: s.state === 'live' ? 'tm-pulse-soft 1.8s ease-in-out infinite' : 'none',
            flexShrink: 0,
          }}
        />
        <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 12, color: 'var(--tm-t1)', letterSpacing: 0.2 }}>
          {shortLabel}
        </span>
        <ChevronRight size={12} style={{ color: 'var(--tm-t5, var(--tm-t4))', opacity: 0.5 }} />
        <span style={{ marginLeft: 'auto', fontSize: 9.5, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--tm-t4)' }}>
          {s.role}
        </span>
      </div>

      {liveExec.length === 0 && awaiting.length === 0 ? (
        <div style={{ fontSize: 11.5, color: 'var(--tm-t4)', fontStyle: 'italic' }}>
          {s.role === 'coordinator' ? 'routing' : s.role === 'evaluator' ? 'watching' : 'idle'}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {liveExec.map((c) => (
            <div key={c.taskId} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span style={{ fontSize: 11.5, color: 'var(--tm-t2)', lineHeight: 1.25 }}>{c.title}</span>
              <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>
                {c.trigger} · {fmt.duration(c.startedAgeS)}
              </span>
            </div>
          ))}
          {awaiting.map((c) => (
            <div key={c.taskId} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span
                style={{
                  fontFamily: 'var(--tm-mono)',
                  fontSize: 9.5,
                  color: 'var(--tm-info-fg)',
                  border: '1px solid var(--tm-info-edge)',
                  background: 'var(--tm-info-bg)',
                  borderRadius: 3,
                  padding: '1px 5px',
                  letterSpacing: 0.3,
                }}
              >
                awaiting merge
              </span>
              <span style={{ fontSize: 10.5, color: 'var(--tm-t3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.title}
              </span>
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: 'auto', display: 'flex', gap: 12, fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>
        <span title="initial / rework / review today">
          <span style={{ color: 'var(--tm-t2)' }}>{s.today.initial}</span>·
          <span style={{ color: 'var(--tm-warn-fg)' }}>{s.today.rework}</span>·
          <span style={{ color: 'var(--tm-info-fg)' }}>{s.today.review}</span>
        </span>
        <span style={{ marginLeft: 'auto' }}>seen {fmt.duration(s.lastEventAgeS)}</span>
      </div>
    </div>
  );
}

function FeedRail({ events }: { events: FeedEvent[] }) {
  return (
    <Panel
      title={
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, width: '100%' }}>
          <Activity size={12} style={{ opacity: 0.7 }} />
          loop activity
        </span>
      }
      padded={false}
      style={{ position: 'sticky', top: 8, maxHeight: 'calc(100vh - 160px)', display: 'flex', flexDirection: 'column' }}
    >
      <div className="tm-scroll" style={{ overflow: 'auto' }}>
        {events.map((e) => (
          <FeedRow key={e.id} e={e} />
        ))}
        {events.length === 0 && (
          <div style={{ padding: 20, textAlign: 'center', fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t4)' }}>
            no recent activity
          </div>
        )}
      </div>
    </Panel>
  );
}

function FeedRow({ e }: { e: FeedEvent }) {
  const navigate = useNavigate();
  const tone = FEED_TONE[e.kind];
  const Icon = FEED_ICON[e.kind];
  const isDigest = e.kind === 'digest';
  // A loop-activity event lands on the loop detail of the task its step
  // belongs to (/tasks/:taskId), not the board. Digests have no task.
  const target = e.taskId ? `/tasks/${e.taskId}` : null;
  return (
    <div
      onClick={target ? () => navigate(target) : undefined}
      style={{
        display: 'flex',
        gap: 9,
        padding: '9px 12px',
        borderBottom: '1px solid var(--tm-border)',
        background: isDigest ? 'var(--tm-surface)' : 'transparent',
        opacity: isDigest ? 0.85 : 1,
        cursor: target ? 'pointer' : 'default',
      }}
      onMouseEnter={(e2) => target && (e2.currentTarget.style.background = 'var(--tm-hover)')}
      onMouseLeave={(e2) => (e2.currentTarget.style.background = isDigest ? 'var(--tm-surface)' : 'transparent')}
    >
      <Icon size={13} style={{ color: `var(--tm-${tone}-fg)`, marginTop: 2, flexShrink: 0 }} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
        <span style={{ fontSize: 11.5, color: isDigest ? 'var(--tm-t3)' : 'var(--tm-t1)', lineHeight: 1.3, fontStyle: isDigest ? 'italic' : 'normal' }}>
          {e.summary}
        </span>
        <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-t4)', display: 'flex', gap: 7, alignItems: 'center' }}>
          <span>{e.action}</span>
          <span>·</span>
          <span>{fmt.duration(e.ageS)}</span>
          {e.taskId && <span style={{ color: 'var(--tm-info-fg)' }}>loop →</span>}
          {e.runUrl && (
            <a href={e.runUrl} target="_blank" rel="noreferrer" onClick={(ev) => ev.stopPropagation()} style={{ color: 'var(--tm-info-fg)', textDecoration: 'none', marginLeft: 'auto' }}>
              run ↗
            </a>
          )}
        </span>
      </div>
    </div>
  );
}
