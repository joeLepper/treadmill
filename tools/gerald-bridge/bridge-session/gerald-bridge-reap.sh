#!/usr/bin/env bash
# ExecStopPost: reap the gerald-bridge tmux session (it lives outside the unit cgroup).
tmux kill-session -t gxbridge 2>/dev/null || true
exit 0
