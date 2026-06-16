/**
 * Doc content — ADR/plan markdown behind the doc reader.
 *
 * Loaded from a LOCAL, GIT-IGNORED fixtures dir (`src/fixtures/docs/`) via
 * `import.meta.glob`, so the bundle is empty on a fresh clone (the reader
 * shows an empty state) and only an operator's local snapshot populates it.
 * The fixtures are gitignored because they are real ADR/plan docs that must
 * not land in this public repo (RAMJAC confidentiality). The durable path
 * is a docs read-through endpoint; this is the local-only interim.
 *
 * Keyed by the doc id = the markdown filename without extension.
 */

const docModules = import.meta.glob('../fixtures/docs/*.md', {
  eager: true,
  query: '?raw',
  import: 'default',
});

export const DOC_CONTENT: Record<string, string | undefined> = {};
for (const path in docModules) {
  const id = path.split('/').pop()!.replace(/\.md$/, '');
  DOC_CONTENT[id] = docModules[path] as string;
}

// ─── Real ledger entries derived from the bundled doc files ──────────
// The ADR/plan list parsed from the actual markdown (H1 title + Status
// frontmatter) — real content. Workflow metadata (owner/reviewer/PR) lives
// in git, not these files, so those stay unset under live derivation.

import type { LedgerDoc, IntentStage } from './v2mock';

function stageFromStatus(s: string): IntentStage {
  const t = s.toLowerCase();
  if (t.includes('accept') || t.includes('merged')) return 'merged';
  if (t.includes('supersed') || t.includes('reject')) return 'done';
  if (t.includes('propos') || t.includes('review')) return 'review';
  if (t.includes('active') || t.includes('execut') || t.includes('submitted')) return 'executing';
  return 'draft';
}

export const realLedger: LedgerDoc[] = Object.entries(DOC_CONTENT)
  .filter(([, src]) => !!src)
  .map(([id, src]) => {
    const body = src!;
    const title = body.match(/^#\s+(.+)$/m)?.[1]?.trim() ?? id;
    const status = body.match(/\*\*Status:\*\*\s*([^\n]+)/)?.[1] ?? '';
    const kind: LedgerDoc['kind'] = /adr/i.test(id) || /^ADR[-\s]/i.test(title) ? 'ADR' : 'Plan';
    return {
      id, kind, title,
      repo: 'joeLepper/treadmill',
      owner: '—', reviewer: '—',
      stage: stageFromStatus(status),
      updatedAgeS: 0,
    };
  });
