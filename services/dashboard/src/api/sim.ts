/**
 * useLiveSim — drives the dashboard's freshness signal.
 *
 * Phase 2 swap: replaces the phase-1 `setInterval` simulation with a
 * real WebSocket subscription against
 * `${WS_BASE}/api/v1/dashboard/ws/events`. The hook's return shape is
 * unchanged (`tick`, `mode`, `lastUpdated`, `flashIds`) so page
 * components and `<ConnectionAffordance>` stay untouched.
 *
 * Behavior:
 *   - mode: 'ws' once the socket opens; 'disconnected' on close/error.
 *   - auto-reconnect with exponential backoff (1s → 2s → 4s … capped 30s).
 *   - flashIds: each event message contributing a `task_id` lands in the
 *     set; cleared 1.5s later (matches the phase-1 visual).
 *   - lastUpdated: refreshes on every incoming message + once per second
 *     so the "updated HH:MM:SS" label stays current without traffic.
 *   - tick: increments on every event (drives reactive recompute).
 */

import { useEffect, useRef, useState } from 'react';
import { fmt } from '../design/fmt';
import type { FreshnessMode } from '../design/ConnectionAffordance';

export interface LiveSim {
  tick: number;
  mode: FreshnessMode;
  lastUpdated: string;
  flashIds: Set<string>;
}

/** Reconnect backoff: 1s → 2s → 4s → … capped at 30s. */
const BACKOFF_START_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
/** Flash visual lifetime per task id — matches the phase-1 sim. */
const FLASH_LIFETIME_MS = 1_500;

interface EventMessage {
  type: 'event';
  id?: string;
  entity_type?: string;
  action?: string;
  task_id?: string | null;
  ts?: string;
}

interface HelloMessage {
  type: 'hello';
  ts?: string;
}

interface HeartbeatMessage {
  type: 'heartbeat';
  ts?: string;
}

type ServerMessage = EventMessage | HelloMessage | HeartbeatMessage;

function wsUrl(): string {
  // Derive from window.location so the dashboard works behind both the
  // dev-server proxy (ws://localhost:5173) and nginx in front of the API
  // (wss:// in deployed setups).
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/api/v1/dashboard/ws/events`;
}

export function useLiveSim({ enabled = true } = {}): LiveSim {
  const [tick, setTick] = useState(0);
  const [mode, setMode] = useState<FreshnessMode>('disconnected');
  const [lastUpdated, setLastUpdated] = useState(fmt.time());
  const [flashIds, setFlashIds] = useState<Set<string>>(new Set());
  const flashRef = useRef<Set<string>>(new Set());
  const flashTimers = useRef<Map<string, number>>(new Map());

  // Clock tick — once per second; cheap, keeps "updated HH:MM:SS" alive
  // even when the event stream is quiet.
  useEffect(() => {
    const id = window.setInterval(() => {
      setLastUpdated(fmt.time());
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setMode('disconnected');
      return;
    }

    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let backoff = BACKOFF_START_MS;
    let disposed = false;

    const scheduleReconnect = () => {
      if (disposed) return;
      const delay = backoff;
      backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
      reconnectTimer = window.setTimeout(connect, delay);
    };

    const handleEvent = (data: EventMessage) => {
      const taskId = data.task_id;
      if (taskId) {
        flashRef.current.add(taskId);
        setFlashIds(new Set(flashRef.current));
        // Clear this id 1.5s after it landed. A second event on the
        // same id resets the timer so the row stays lit through bursts.
        const existing = flashTimers.current.get(taskId);
        if (existing !== undefined) {
          window.clearTimeout(existing);
        }
        const timerId = window.setTimeout(() => {
          flashRef.current.delete(taskId);
          flashTimers.current.delete(taskId);
          setFlashIds(new Set(flashRef.current));
        }, FLASH_LIFETIME_MS);
        flashTimers.current.set(taskId, timerId);
      }
      setTick((t) => t + 1);
    };

    const connect = () => {
      if (disposed) return;
      reconnectTimer = null;
      try {
        socket = new WebSocket(wsUrl());
      } catch {
        // Some test environments throw synchronously when WebSocket
        // construction fails (e.g. invalid URL). Schedule a retry.
        scheduleReconnect();
        return;
      }
      socket.onopen = () => {
        backoff = BACKOFF_START_MS;
        setMode('ws');
        setLastUpdated(fmt.time());
      };
      socket.onmessage = (evt: MessageEvent) => {
        setLastUpdated(fmt.time());
        let data: ServerMessage;
        try {
          data = JSON.parse(evt.data as string) as ServerMessage;
        } catch {
          return;
        }
        if (data.type === 'event') {
          handleEvent(data);
        }
        // hello / heartbeat: lastUpdated already bumped above; nothing
        // else to do.
      };
      socket.onerror = () => {
        // onerror always precedes onclose; let onclose drive state.
      };
      socket.onclose = () => {
        socket = null;
        if (disposed) return;
        setMode('disconnected');
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      flashTimers.current.forEach((t) => window.clearTimeout(t));
      flashTimers.current.clear();
      if (socket !== null) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        try {
          socket.close();
        } catch {
          /* socket already torn down */
        }
        socket = null;
      }
    };
  }, [enabled]);

  return { tick, mode, lastUpdated, flashIds };
}
