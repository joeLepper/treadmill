/**
 * Query hooks the pages consume.
 *
 * Phase 1 (this PR): the `queryFn` bodies call the in-process mock
 * (`mock.ts`). They still go through TanStack Query so the cache /
 * refetch / stale-time machinery is in place from day one.
 *
 * Phase 2 (follow-up PR): swap each `queryFn` body for a `fetch` call
 * against `services/api/treadmill_api/routers/dashboard.py`. Page
 * components do not change.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { Bucket, Task } from './types';
import {
  ACCOUNTS,
  acknowledgeEscalation as mockAck,
  bucketCounts as mockBucketCounts,
  cancelTask as mockCancel,
  FLEET,
  getEscalations as mockEscalations,
  getEvents as mockEvents,
  getNonTerminalTasks as mockTasks,
  getRepoDocs as mockRepoDocs,
  getTaskDetail as mockTaskDetail,
} from './mock';

const STALE_MS = 3_000;

export interface OverviewFilters {
  repo?: string;
  bucket?: Bucket;
  account?: string;
  q?: string;
}

export function useOverview(filters: OverviewFilters = {}) {
  return useQuery({
    queryKey: ['overview', filters],
    queryFn: async () => ({
      accounts: ACCOUNTS,
      fleet: FLEET,
      escalations: mockEscalations(),
      tasks: mockTasks(filters),
      bucketCounts: mockBucketCounts(),
      events: mockEvents(),
    }),
    staleTime: STALE_MS,
    refetchInterval: 5_000,
  });
}

export function useTaskDetail(taskId: string) {
  return useQuery({
    queryKey: ['task', taskId],
    queryFn: async () => mockTaskDetail(taskId),
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
    queryFn: async () => mockRepoDocs(repo),
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
