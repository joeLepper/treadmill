#!/usr/bin/env bash
# ExecStopPost: reap Fran's tmux session so a stop/restart leaves no orphan
# (mirrors treadmill-channel-reap; the tmux tree is outside the unit cgroup).
tmux kill-session -t fran 2>/dev/null || true
exit 0
