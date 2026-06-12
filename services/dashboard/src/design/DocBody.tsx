/**
 * <DocBody> — renders an ADR/plan doc: prose as styled markdown, and any
 * `sequence_of_work` YAML block as visual task cards (not raw YAML).
 *
 * The doc is split on ```yaml fences; a fence whose parsed body contains
 * `sequence_of_work` is rendered as the task board for that plan, inline
 * at its position. Everything else is markdown (react-markdown + GFM).
 */

import { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import yaml from 'js-yaml';
import { ChevronDown, ChevronRight, FileCode2, GitBranch, ShieldCheck } from 'lucide-react';
import type { Tone } from './fmt';

interface ParsedTask {
  id?: string;
  title?: string;
  workflow?: string;
  depends_on?: string[];
  intent?: string;
  scope?: { files?: string[]; services_affected?: string[]; out_of_scope?: string[] };
  validation?: { kind?: string; description?: string; severity?: string }[];
}

type Segment = { kind: 'md'; text: string } | { kind: 'tasks'; tasks: ParsedTask[] };

const YAML_FENCE = /```ya?ml\s*\n([\s\S]*?)```/g;

function splitDoc(src: string): Segment[] {
  const segs: Segment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  YAML_FENCE.lastIndex = 0;
  while ((m = YAML_FENCE.exec(src)) !== null) {
    let parsed: unknown;
    try {
      parsed = yaml.load(m[1]);
    } catch {
      parsed = null;
    }
    const sow = (parsed as { sequence_of_work?: ParsedTask[] } | null)?.sequence_of_work;
    if (Array.isArray(sow)) {
      if (m.index > last) segs.push({ kind: 'md', text: src.slice(last, m.index) });
      segs.push({ kind: 'tasks', tasks: sow });
      last = m.index + m[0].length;
    }
  }
  if (last < src.length) segs.push({ kind: 'md', text: src.slice(last) });
  return segs.length ? segs : [{ kind: 'md', text: src }];
}

/** Strip a leading YAML frontmatter block (`---\n…\n---`) — it is metadata
 *  the header already surfaces, and react-markdown would render it as a
 *  stray rule + heading. */
function stripFrontmatter(src: string): string {
  return src.replace(/^﻿?\s*---\n[\s\S]*?\n---\s*\n/, '');
}

export function DocBody({ source }: { source: string }) {
  const segments = useMemo(() => splitDoc(stripFrontmatter(source)), [source]);
  return (
    <div>
      {segments.map((seg, i) =>
        seg.kind === 'md' ? (
          <div key={i} className="tm-md">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{seg.text}</ReactMarkdown>
          </div>
        ) : (
          <TaskBoard key={i} tasks={seg.tasks} />
        ),
      )}
    </div>
  );
}

function TaskBoard({ tasks }: { tasks: ParsedTask[] }) {
  return (
    <div style={{ margin: '6px 0 20px' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 10,
          fontFamily: 'var(--tm-mono)',
          fontSize: 11,
          letterSpacing: 0.6,
          textTransform: 'uppercase',
          color: 'var(--tm-t3)',
        }}
      >
        <Workflow /> sequence of work · {tasks.length} task{tasks.length === 1 ? '' : 's'}
      </div>
      <div style={{ display: 'grid', gap: 10 }}>
        {tasks.map((t, i) => (
          <TaskCard key={t.id ?? i} task={t} index={i} />
        ))}
      </div>
    </div>
  );
}

function Workflow() {
  return <GitBranch size={13} style={{ transform: 'rotate(90deg)', opacity: 0.7 }} />;
}

function TaskCard({ task, index }: { task: ParsedTask; index: number }) {
  const [open, setOpen] = useState(false);
  const files = task.scope?.files ?? [];
  const deps = task.depends_on ?? [];
  const vals = task.validation ?? [];
  const intent = (task.intent ?? '').trim();
  const intentPreview = intent.split('\n').filter(Boolean)[0] ?? '';

  return (
    <div style={{ border: '1px solid var(--tm-border)', borderRadius: 3, background: 'var(--tm-surface)', overflow: 'hidden' }}>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'flex-start',
          gap: 11,
          padding: '12px 14px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
          color: 'inherit',
        }}
      >
        <span style={{ marginTop: 1, color: 'var(--tm-t4)' }}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
        <span
          className="tm-tnum"
          style={{
            fontFamily: 'var(--tm-mono)',
            fontSize: 11,
            color: 'var(--tm-t4)',
            border: '1px solid var(--tm-border-2)',
            borderRadius: 3,
            padding: '1px 6px',
            marginTop: 1,
            flexShrink: 0,
          }}
        >
          {String(index + 1).padStart(2, '0')}
        </span>
        <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column', gap: 5 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 9, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13, color: 'var(--tm-t1)', fontWeight: 500 }}>{task.title ?? task.id}</span>
            {task.workflow && <Chip tone="info" mono>{task.workflow}</Chip>}
          </div>
          {task.id && (
            <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)' }}>{task.id}</span>
          )}
          {!open && intentPreview && (
            <span style={{ fontSize: 11.5, color: 'var(--tm-t3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {intentPreview}
            </span>
          )}
          {/* meta strip */}
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontFamily: 'var(--tm-mono)', fontSize: 10, color: 'var(--tm-t4)', marginTop: 1 }}>
            {deps.length > 0 && <span><GitBranch size={9} style={{ verticalAlign: -1 }} /> {deps.length} dep{deps.length === 1 ? '' : 's'}</span>}
            {files.length > 0 && <span><FileCode2 size={9} style={{ verticalAlign: -1 }} /> {files.length} path{files.length === 1 ? '' : 's'}</span>}
            {vals.length > 0 && <span><ShieldCheck size={9} style={{ verticalAlign: -1 }} /> {vals.length} gate{vals.length === 1 ? '' : 's'}</span>}
          </div>
        </div>
      </button>

      {open && (
        <div style={{ padding: '4px 14px 14px 39px', display: 'flex', flexDirection: 'column', gap: 13, borderTop: '1px solid var(--tm-border)' }}>
          {deps.length > 0 && (
            <Field label="depends on">
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {deps.map((d) => <Chip key={d} tone="warn" mono>{d}</Chip>)}
              </div>
            </Field>
          )}
          {intent && (
            <Field label="intent">
              <pre
                className="tm-scroll"
                style={{
                  margin: 0,
                  whiteSpace: 'pre-wrap',
                  fontFamily: 'var(--tm-sans)',
                  fontSize: 12,
                  lineHeight: 1.55,
                  color: 'var(--tm-t2)',
                  background: 'var(--tm-bg)',
                  border: '1px solid var(--tm-border)',
                  borderRadius: 3,
                  padding: '10px 12px',
                  maxHeight: 280,
                  overflow: 'auto',
                }}
              >
                {intent}
              </pre>
            </Field>
          )}
          {files.length > 0 && (
            <Field label="scope · files">
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {files.map((f) => <Chip key={f} tone="muted" mono><FileCode2 size={9} style={{ verticalAlign: -1, marginRight: 3 }} />{f}</Chip>)}
              </div>
            </Field>
          )}
          {vals.length > 0 && (
            <Field label="validation gates">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {vals.map((v, i) => (
                  <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5, color: 'var(--tm-t3)' }}>
                    <Chip tone={v.kind === 'llm-judge' ? 'info' : 'ok'} mono>{v.kind ?? 'check'}</Chip>
                    {v.severity && <Chip tone={v.severity === 'blocking' ? 'danger' : 'muted'} mono>{v.severity}</Chip>}
                    <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {(v.description ?? '').split('\n')[0]}
                    </span>
                  </div>
                ))}
              </div>
            </Field>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
      {children}
    </div>
  );
}

function Chip({ children, tone = 'muted', mono }: { children: React.ReactNode; tone?: Tone; mono?: boolean }) {
  return (
    <span
      style={{
        fontFamily: mono ? 'var(--tm-mono)' : 'var(--tm-sans)',
        fontSize: 10.5,
        color: `var(--tm-${tone}-fg)`,
        background: `var(--tm-${tone}-bg)`,
        border: `1px solid var(--tm-${tone}-edge)`,
        borderRadius: 4,
        padding: '2px 7px',
        display: 'inline-flex',
        alignItems: 'center',
      }}
    >
      {children}
    </span>
  );
}
