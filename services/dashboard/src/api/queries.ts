/**
 * Query hooks the pages consume.
 *
 * READ hooks (`useOverview`, `useTaskDetail`, `useRepoDocs`) fetch live
 * data from `services/api/treadmill_api/routers/dashboard/*.py`. Response
 * shapes match `./types.ts` field-for-field — the page components don't
 * know or care that the seam moved from in-process mock to HTTP.
 *
 * Mutation hooks (`useCancelTask`, `useAcknowledgeEscalation`) still call
 * the in-process mock; their HTTP swap lands with the cancel / ack
 * endpoints in a follow-up PR.
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
} from './types';
import {
  acknowledgeEscalation as mockAck,
  cancelTask as mockCancel,
} from './mock';

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
      mockCancel(taskId, reason);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['overview'] });
      qc.invalidateQueries({ queryKey: ['task'] });
    },
  });
}

export function useAcknowledgeEscalation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId }: { taskId: string }) => {
      mockAck(taskId);
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
