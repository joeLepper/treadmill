#!/usr/bin/env bash
# ExecStopPost: reap Gerald's tmux session so a stop/restart leaves no orphan
# (the tmux tree is outside the unit cgroup). Mirrors fran-reap.
tmux kill-session -t gerald 2>/dev/null || true
exit 0
