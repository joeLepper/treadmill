/**
 * Query hooks the pages consume.
 *
 * Every read + mutation hook goes through `_apiFetch` against
 * `services/api/treadmill_api/routers/dashboard/*.py`. Response and
 * request shapes match `./types.ts` field-for-field — the page
 * components don't know or care that the seam moved from in-process
 * mock to HTTP.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  Bucket,
  Escalation,
  Event,
  Account,
  Fleet,
  RepoDocs,
  Task,
  TaskDetail,
  TriageFinding,
  TriageLabelInput,
} from './types';
const STALE_MS = 3_000;

async function _apiFetch<T>(url: string): Promise<T> {
  const res = await fetch(url, {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export interface OverviewFilters {
  repo?: string;
  bucket?: Bucket;
  account?: string;
  q?: string;
}

interface BucketCounts {
  blocked: number;
  inflight: number;
  hopper: number;
  total: number;
}

interface OverviewResponse {
  accounts: Account[];
  fleet: Fleet;
  escalations: Escalation[];
  tasks: Task[];
  bucketCounts: BucketCounts;
  events: Event[];
}

export function useOverview(filters: OverviewFilters = {}) {
  return useQuery({
    queryKey: ['overview', filters],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (filters.repo) params.set('repo', filters.repo);
      if (filters.bucket) params.set('bucket', filters.bucket);
      if (filters.account) params.set('account', filters.account);
      if (filters.q) params.set('q', filters.q);
      const qs = params.toString();
      const url = qs
        ? `/api/v1/dashboard/overview?${qs}`
        : '/api/v1/dashboard/overview';
      return _apiFetch<OverviewResponse>(url);
    },
    staleTime: STALE_MS,
    refetchInterval: 5_000,
  });
}

export function useTaskDetail(taskId: string) {
  return useQuery({
    queryKey: ['task', taskId],
    queryFn: async () =>
      _apiFetch<TaskDetail>('/api/v1/dashboard/tasks/' + taskId),
    staleTime: STALE_MS,
    refetchInterval: (query) => {
      // Poll only while the latest run is still active.
      const data = query.state.data;
      if (!data) return 5_000;
      const last = data.runs[data.runs.length - 1];
      return last?.status === 'running' ? 3_000 : false;
    },
  });
}

export function useRepoDocs(repo: string) {
  return useQuery({
    queryKey: ['repo-docs', repo],
    queryFn: async () =>
      _apiFetch<RepoDocs>(
        '/api/v1/dashboard/repos/' + encodeURIComponent(repo) + '/docs',
      ),
    staleTime: 60_000,
  });
}

/* ─── Actions ─────────────────────────────────────────────────────── */

export function useCancelTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId, reason }: { taskId: string; reason: string }) => {
      const res = await fetch(`/api/v1/dashboard/tasks/${taskId}/cancel`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      if (!res.ok) {
        throw new Error(`cancel task failed: HTTP ${res.status}`);
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['overview'] });
      qc.invalidateQueries({ queryKey: ['task'] });
    },
  });
}

/* ─── Triage labeling (ADR-0061) ───────────────────────────────────── */

const UNLABELED_KEY = ['triage', 'unlabeled'] as const;

export function useUnlabeledFindings() {
  return useQuery({
    queryKey: UNLABELED_KEY,
    queryFn: async () =>
      _apiFetch<TriageFinding[]>(
        '/api/v1/triage/findings?label_is_real_bug=null&limit=50',
      ),
    staleTime: STALE_MS,
  });
}

export function useLabelFinding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      findingId,
      label,
    }: {
      findingId: string;
      label: TriageLabelInput;
    }) => {
      const res = await fetch(`/api/v1/triage/findings/${findingId}/label`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(label),
      });
      if (!res.ok) {
        throw new Error(`label finding failed: HTTP ${res.status}`);
      }
      return (await res.json()) as TriageFinding;
    },
    // Optimistic: drop the labeled finding out of the unlabeled cache
    // so the UI flips to the next finding without waiting for a refetch.
    onMutate: async ({ findingId }) => {
      await qc.cancelQueries({ queryKey: UNLABELED_KEY });
      const prev = qc.getQueryData<TriageFinding[]>(UNLABELED_KEY);
      qc.setQueryData<TriageFinding[] | undefined>(UNLABELED_KEY, (old) =>
        old?.filter((f) => f.finding_id !== findingId),
      );
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(UNLABELED_KEY, ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: UNLABELED_KEY });
    },
  });
}

export function useAcknowledgeEscalation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId }: { taskId: string }) => {
      const res = await fetch(
        `/api/v1/dashboard/tasks/${taskId}/ack-escalation`,
        { method: 'POST' },
      );
      if (!res.ok) {
        throw new Error(`acknowledge escalation failed: HTTP ${res.status}`);
      }
    },
    // Optimistic — clear the escalation immediately, rollback on error.
    onMutate: async ({ taskId }) => {
      await qc.cancelQueries({ queryKey: ['overview'] });
      const prev = qc.getQueriesData<{ tasks: Task[] }>({ queryKey: ['overview'] });
      qc.setQueriesData<any>({ queryKey: ['overview'] }, (old: any) => {
        if (!old) return old;
        return {
          ...old,
          escalations: old.escalations.filter((e: any) => e.task_id !== taskId),
          tasks: old.tasks.map((t: Task) =>
            t.id === taskId ? { ...t, escalated: false } : t,
          ),
        };
      });
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      ctx?.prev?.forEach(([key, data]) => qc.setQueryData(key, data));
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['overview'] });
    },
  });
}
