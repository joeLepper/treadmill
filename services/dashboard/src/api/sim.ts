/**
 * useLiveSim — simulates the WS-driven freshness signal.
 *
 * Ports the `useLiveSim` hook from the Claude Design bundle. In phase 1
 * (mock-only) it advances internal state every ~3s; in phase 2 we
 * replace this with a real WebSocket subscription against `/ws/events`,
 * exposing the same shape (`mode`, `lastUpdated`, `flashIds`, `tick`).
 *
 * `flashIds` is the set of task IDs whose row in the overview table
 * should flash on the next render — visual confirmation of "this thing
 * just changed."
 */

import { useEffect, useReducer, useRef, useState } from 'react';
import { fmt } from '../design/fmt';
import { _simAdvance } from './mock';
import type { FreshnessMode } from '../design/ConnectionAffordance';

export interface LiveSim {
  tick: number;
  mode: FreshnessMode;
  lastUpdated: string;
  flashIds: Set<string>;
}

export function useLiveSim({ enabled = true, intervalMs = 3500 } = {}): LiveSim {
  const [, force] = useReducer((x: number) => x + 1, 0);
  const [tick, setTick] = useState(0);
  const [mode] = useState<FreshnessMode>('ws');
  const [lastUpdated, setLastUpdated] = useState(fmt.time());
  const [flashIds, setFlashIds] = useState<Set<string>>(new Set());
  const flashRef = useRef<Set<string>>(new Set());

  // Clock tick — once per second; cheap, keeps "updated HH:MM:SS" alive.
  useEffect(() => {
    const id = window.setInterval(() => {
      setLastUpdated(fmt.time());
      setTick((t) => t + 1);
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  // Sim tick — push events / advance tasks every ~intervalMs.
  useEffect(() => {
    if (!enabled) return;
    const id = window.setInterval(() => {
      const { liveTaskId } = _simAdvance();
      if (liveTaskId) flashRef.current.add(liveTaskId);
      setFlashIds(new Set(flashRef.current));
      window.setTimeout(() => {
        flashRef.current.clear();
        setFlashIds(new Set());
      }, 1500);
      force();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [enabled, intervalMs]);

  return { tick, mode, lastUpdated, flashIds };
}
