#!/usr/bin/env bash
# ExecStopPost: reap the fran-bridge tmux session (it lives outside the unit cgroup).
tmux kill-session -t cxbridge 2>/dev/null || true
exit 0
