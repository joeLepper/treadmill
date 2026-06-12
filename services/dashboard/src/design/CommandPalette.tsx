/**
 * <CommandPalette> — the top-bar "jump to" box, wired.
 *
 * Indexes plans, tasks, sessions, and ADRs; type to filter, click or
 * Enter to navigate. ⌘K / Ctrl-K focuses it from anywhere. Replaces the
 * decorative search box. Targets honor the URL-state convention (a session
 * hit deep-links the Mission Control worker drawer).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search } from 'lucide-react';
import { plans, pipeline, teams, ledger } from '../api/v2mock';

interface Entry { kind: string; label: string; sub: string; path: string; }

function buildIndex(): Entry[] {
  const out: Entry[] = [];
  const seenTask = new Set<string>();
  for (const p of plans) out.push({ kind: 'plan', label: p.title, sub: `plan · ${p.repo}`, path: `/plans/${p.id}` });
  for (const t of pipeline) { seenTask.add(t.id); out.push({ kind: 'task', label: t.title, sub: `task · ${t.worker} · ${t.stage}`, path: `/tasks/${t.id}` }); }
  for (const p of plans) for (const t of p.tasks) if (!seenTask.has(t.id)) { seenTask.add(t.id); out.push({ kind: 'task', label: t.title, sub: `task · ${p.repo}`, path: `/tasks/${t.id}` }); }
  for (const team of teams) for (const s of [team.coordinator, team.evaluator, ...team.workers]) out.push({ kind: 'session', label: s.label, sub: `${s.role} · ${team.repo}`, path: `/?session=${s.label}` });
  for (const d of ledger) if (d.kind === 'ADR') out.push({ kind: 'adr', label: d.title, sub: `ADR · ${d.repo}`, path: `/adrs/${d.id}` });
  return out;
}

const KIND_TONE: Record<string, string> = { plan: 'warn', task: 'info', session: 'ok', adr: 'muted' };

export function CommandPalette() {
  const navigate = useNavigate();
  const index = useMemo(buildIndex, []);
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return [];
    return index
      .filter((e) => e.label.toLowerCase().includes(needle) || e.sub.toLowerCase().includes(needle))
      .slice(0, 8);
  }, [q, index]);

  // ⌘K / Ctrl-K to focus from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        inputRef.current?.focus();
        setOpen(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // close on outside click
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onClick);
    return () => window.removeEventListener('mousedown', onClick);
  }, []);

  function go(e: Entry) {
    setQ('');
    setOpen(false);
    inputRef.current?.blur();
    navigate(e.path);
  }

  function onKeyDown(ev: React.KeyboardEvent) {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); setActive((a) => Math.min(a + 1, results.length - 1)); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (ev.key === 'Enter' && results[active]) { ev.preventDefault(); go(results[active]); }
    else if (ev.key === 'Escape') { setOpen(false); inputRef.current?.blur(); }
  }

  return (
    <div ref={wrapRef} style={{ position: 'relative', flex: 1, maxWidth: 380 }}>
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          background: 'var(--tm-surface-2)', borderRadius: 2,
          border: `1px solid ${open ? 'var(--tm-info-edge)' : 'var(--tm-border)'}`,
          padding: '5px 10px', color: 'var(--tm-t3)', fontSize: 12.5,
        }}
      >
        <Search size={13} />
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => { setQ(e.target.value); setOpen(true); setActive(0); }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          placeholder="jump to task / plan / session / ADR"
          style={{ flex: 1, background: 'transparent', border: 'none', outline: 'none', color: 'var(--tm-t1)', fontSize: 12.5, fontFamily: 'var(--tm-sans)' }}
        />
        <span style={{ fontFamily: 'var(--tm-mono)', color: 'var(--tm-t4)', fontSize: 11 }}>⌘K</span>
      </div>

      {open && results.length > 0 && (
        <div
          className="tm-scroll"
          style={{
            position: 'absolute', top: 'calc(100% + 4px)', left: 0, right: 0, zIndex: 60,
            background: 'var(--tm-surface-2)', border: '1px solid var(--tm-border)', borderRadius: 3,
            boxShadow: '0 12px 36px rgba(0,0,0,0.5)', overflow: 'auto', maxHeight: 360, padding: 4,
          }}
        >
          {results.map((e, i) => {
            const t = KIND_TONE[e.kind] ?? 'muted';
            return (
              <div
                key={e.path + i}
                onMouseEnter={() => setActive(i)}
                onMouseDown={(ev) => { ev.preventDefault(); go(e); }}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '7px 9px', borderRadius: 2, cursor: 'pointer',
                  background: i === active ? 'var(--tm-hover)' : 'transparent',
                }}
              >
                <span style={{ fontFamily: 'var(--tm-mono)', fontSize: 8.5, letterSpacing: 0.4, textTransform: 'uppercase', color: `var(--tm-${t}-fg)`, border: `1px solid var(--tm-${t}-edge)`, background: `var(--tm-${t}-bg)`, borderRadius: 3, padding: '1px 5px', flexShrink: 0, width: 52, textAlign: 'center' }}>{e.kind}</span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 12.5, color: 'var(--tm-t1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.label}</div>
                  <div style={{ fontFamily: 'var(--tm-mono)', fontSize: 9.5, color: 'var(--tm-t4)' }}>{e.sub}</div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {open && q.trim() && results.length === 0 && (
        <div style={{ position: 'absolute', top: 'calc(100% + 4px)', left: 0, right: 0, zIndex: 60, background: 'var(--tm-surface-2)', border: '1px solid var(--tm-border)', borderRadius: 3, padding: '10px 12px', fontFamily: 'var(--tm-mono)', fontSize: 11, color: 'var(--tm-t4)' }}>
          no match for "{q}"
        </div>
      )}
    </div>
  );
}
