/**
 * <StepTranscriptDrawer> — draws the conversation for JUST one loop step.
 *
 * Opened from a journey cycle that carries a `stepId`. The turn stream is
 * the real transcript slice for that task_execution: assistant prose,
 * each tool call (Bash / Read / grep …) with its one-line target, and the
 * tool result (collapsed monospace). This is the answer to "can we draw
 * the conversation for just that step" — yes, sliced by request_id.
 */

import { useState } from 'react';
import {
  X, Terminal, FileText, FileSearch, Search, Pencil, Wrench,
  MessageSquare, CornerDownRight, ListTodo,
} from 'lucide-react';
import type { StepTranscript, StepTurn } from '../api/stepTranscript';
import { fmt } from './fmt';

const TOOL_ICON: Record<string, typeof Wrench> = {
  Bash: Terminal, Read: FileText, Grep: FileSearch, Glob: FileSearch,
  Search: Search, Edit: Pencil, Write: Pencil, MultiEdit: Pencil,
  TaskCreate: ListTodo, TaskUpdate: ListTodo, ToolSearch: Search,
};

export function StepTranscriptDrawer({ step, onClose }: { step: StepTranscript; onClose: () => void }) {
  const totalTok = step.inputTokens + step.outputTokens;
  return (
    <div
      onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 50, display: 'flex', justifyContent: 'flex-end' }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="tm-scroll"
        style={{
          width: 'min(640px, 92vw)', height: '100%', background: 'var(--tm-bg)',
          borderLeft: '1px solid var(--tm-border)', overflow: 'auto', display: 'flex', flexDirection: 'column',
          boxShadow: '-24px 0 60px rgba(0,0,0,0.45)',
        }}
      >
        {/* header — sticky */}
        <div style={{ position: 'sticky', top: 0, background: 'var(--tm-bg)', borderBottom: '1px solid var(--tm-border)', padding: '14px 18px', zIndex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, letterSpacing: 0.6, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>step conversation</div>
              <div style={{ fontSize: 14, color: 'var(--tm-t1)', marginTop: 3 }}>{step.title}</div>
            </div>
            <button onClick={onClose} style={{ background: 'transparent', border: '1px solid var(--tm-border)', borderRadius: 2, color: 'var(--tm-t3)', cursor: 'pointer', padding: 4, display: 'flex' }}>
              <X size={14} />
            </button>
          </div>
          {/* step facts — all real, from the join */}
          <div style={{ display: 'flex', gap: 16, marginTop: 11, flexWrap: 'wrap' }}>
            <HFact label="actor" value={step.actor} mono />
            <HFact label="model" value={step.model.replace('claude-', '')} mono />
            <HFact label="api calls" value={String(step.calls)} />
            <HFact label="tokens" value={fmt.tokens(totalTok)} />
            <HFact label="cache read" value={fmt.tokens(step.cacheReadTokens)} />
            <HFact label="turns" value={String(step.turnCount)} />
          </div>
          <div style={{ marginTop: 9, fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-t4)' }}>
            sliced from session transcript by request_id · te {step.taskExecutionId.slice(0, 8)}
          </div>
        </div>

        {/* turn stream */}
        <div style={{ padding: '12px 18px 40px' }}>
          {step.turns.map((t, i) => <Turn key={i} t={t} />)}
        </div>
      </div>
    </div>
  );
}

function HFact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 8.5, letterSpacing: 0.5, textTransform: 'uppercase', color: 'var(--tm-t4)' }}>{label}</span>
      <span className={mono ? undefined : 'tm-tnum'} style={{ fontSize: 12, color: 'var(--tm-t2)', fontFamily: mono ? 'var(--tm-mono)' : 'var(--tm-sans)' }}>{value}</span>
    </div>
  );
}

function Turn({ t }: { t: StepTurn }) {
  if (t.kind === 'say') {
    return (
      <div style={{ display: 'flex', gap: 9, padding: '7px 0' }}>
        <MessageSquare size={13} style={{ color: 'var(--tm-info-fg)', flexShrink: 0, marginTop: 2 }} />
        <div style={{ fontSize: 12.5, color: 'var(--tm-t1)', lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{t.text}</div>
      </div>
    );
  }
  if (t.kind === 'tool') {
    const Icon = TOOL_ICON[t.name ?? ''] ?? Wrench;
    return (
      <div style={{ display: 'flex', gap: 9, padding: '4px 0 4px 0', alignItems: 'baseline' }}>
        <Icon size={12} style={{ color: 'var(--tm-warn-fg)', flexShrink: 0, transform: 'translateY(2px)' }} />
        <div style={{ minWidth: 0, flex: 1 }}>
          <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-warn-fg)', marginRight: 8 }}>{t.name}</span>
          {t.hint && <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t3)', wordBreak: 'break-word' }}>{t.hint}</span>}
        </div>
      </div>
    );
  }
  return <Result t={t} />;
}

function Result({ t }: { t: StepTurn }) {
  const [open, setOpen] = useState(false);
  const text = t.text ?? '';
  const oneLine = text.replace(/\s+/g, ' ').trim();
  if (!oneLine) return null;
  return (
    <div style={{ display: 'flex', gap: 9, padding: '1px 0 6px 0', alignItems: 'baseline' }}>
      <CornerDownRight size={11} style={{ color: 'var(--tm-t4)', flexShrink: 0, transform: 'translateY(2px)' }} />
      <button
        onClick={() => setOpen((v) => !v)}
        style={{ textAlign: 'left', background: 'transparent', border: 'none', padding: 0, cursor: text.length > 90 ? 'pointer' : 'default', minWidth: 0, flex: 1 }}
      >
        <div
          style={{
            fontFamily: 'var(--tm-mono)', fontSize: 10.5, color: 'var(--tm-t4)', lineHeight: 1.45,
            whiteSpace: open ? 'pre-wrap' : 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            wordBreak: open ? 'break-word' : 'normal',
            borderLeft: '2px solid var(--tm-border-2)', paddingLeft: 8,
          }}
        >
          {open ? text : oneLine}
        </div>
        {(t.truncated || text.length > 90) && (
          <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 9, color: 'var(--tm-t4)', paddingLeft: 10 }}>
            {open ? '⌃ collapse' : t.truncated ? '⌄ output truncated' : '⌄ expand'}
          </span>
        )}
      </button>
    </div>
  );
}
