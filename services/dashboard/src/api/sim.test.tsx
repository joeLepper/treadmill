/**
 * Tests for `useLiveSim` — the WebSocket-backed freshness signal that
 * drives `<ConnectionAffordance>` and the row-flash visual.
 *
 * Stubs `window.WebSocket` with a controllable fake so we can drive
 * onopen / onmessage / onclose synchronously and assert hook state
 * transitions without a real server.
 *
 * Coverage:
 *   - mode flips to 'ws' on socket open.
 *   - an event message with a `task_id` adds the id to `flashIds`.
 *   - lastUpdated changes on each incoming message.
 *   - on close, mode flips to 'disconnected' and a reconnect is
 *     attempted after the backoff window.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useLiveSim } from './sim';

/**
 * Minimal `WebSocket` fake. Only the lifecycle hooks the hook reads
 * are implemented; everything else is stubbed.
 */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  onopen: ((this: WebSocket, ev: Event) => void) | null = null;
  onclose: ((this: WebSocket, ev: CloseEvent) => void) | null = null;
  onerror: ((this: WebSocket, ev: Event) => void) | null = null;
  onmessage: ((this: WebSocket, ev: MessageEvent) => void) | null = null;
  closeCalls = 0;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(_data: string): void {
    /* hook never sends */
  }

  close(): void {
    this.closeCalls += 1;
  }

  /* Test helpers — not on the real interface. */
  emitOpen(): void {
    this.onopen?.call(this as unknown as WebSocket, new Event('open'));
  }

  emitMessage(payload: unknown): void {
    const evt = { data: JSON.stringify(payload) } as MessageEvent;
    this.onmessage?.call(this as unknown as WebSocket, evt);
  }

  emitClose(): void {
    const evt = { code: 1006 } as CloseEvent;
    this.onclose?.call(this as unknown as WebSocket, evt);
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.useFakeTimers();
  // window.location.protocol is read by the hook to pick ws vs wss.
  // jsdom defaults to 'http:' which gives ws:// — fine for the test.
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('useLiveSim', () => {
  it('starts disconnected and flips to ws on socket open', () => {
    const { result } = renderHook(() => useLiveSim());

    // A socket was constructed immediately; mode is still 'disconnected'
    // until onopen fires.
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(result.current.mode).toBe('disconnected');

    act(() => {
      FakeWebSocket.instances[0]!.emitOpen();
    });

    expect(result.current.mode).toBe('ws');
  });

  it('adds task_id to flashIds on an event message', () => {
    const { result } = renderHook(() => useLiveSim());
    act(() => {
      FakeWebSocket.instances[0]!.emitOpen();
    });

    act(() => {
      FakeWebSocket.instances[0]!.emitMessage({
        type: 'event',
        id: 'evt_1',
        entity_type: 'task',
        action: 'registered',
        task_id: 'tsk_abc',
        ts: '2026-05-27T12:00:00Z',
      });
    });

    expect(result.current.flashIds.has('tsk_abc')).toBe(true);
    expect(result.current.tick).toBeGreaterThan(0);

    // The flash visual is transient — after 1.5s the id drops out.
    act(() => {
      vi.advanceTimersByTime(1_500);
    });
    expect(result.current.flashIds.has('tsk_abc')).toBe(false);
  });

  it('updates lastUpdated on incoming messages', () => {
    const { result } = renderHook(() => useLiveSim());
    act(() => {
      FakeWebSocket.instances[0]!.emitOpen();
    });
    const initial = result.current.lastUpdated;

    // Advance wall-clock so the next fmt.time() snapshot differs.
    act(() => {
      vi.setSystemTime(new Date('2026-05-27T12:01:23Z'));
      FakeWebSocket.instances[0]!.emitMessage({
        type: 'heartbeat',
        ts: '2026-05-27T12:01:23Z',
      });
    });

    expect(result.current.lastUpdated).not.toBe(initial);
  });

  it('flips to disconnected on close and reconnects after backoff', () => {
    const { result } = renderHook(() => useLiveSim());
    act(() => {
      FakeWebSocket.instances[0]!.emitOpen();
    });
    expect(result.current.mode).toBe('ws');

    act(() => {
      FakeWebSocket.instances[0]!.emitClose();
    });
    expect(result.current.mode).toBe('disconnected');
    // Still only one socket — reconnect is scheduled, not immediate.
    expect(FakeWebSocket.instances).toHaveLength(1);

    // Backoff starts at 1s; fire the timer and we should see a new
    // socket constructed.
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('ignores event messages without a task_id (e.g. system events)', () => {
    const { result } = renderHook(() => useLiveSim());
    act(() => {
      FakeWebSocket.instances[0]!.emitOpen();
      FakeWebSocket.instances[0]!.emitMessage({
        type: 'event',
        id: 'evt_2',
        entity_type: 'schedule',
        action: 'tick',
        task_id: null,
        ts: '2026-05-27T12:00:00Z',
      });
    });

    expect(result.current.flashIds.size).toBe(0);
    // Tick still bumps so reactive queries refetch even without a task.
    expect(result.current.tick).toBeGreaterThan(0);
  });
});
