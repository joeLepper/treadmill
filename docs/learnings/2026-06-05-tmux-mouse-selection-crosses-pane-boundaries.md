---
date: 2026-06-05
trigger: correction
status: captured
related: plan-2026-06-05-cc-relay-trust-gates
---

# Learning: tmux mouse selection crosses pane boundaries

## Trigger

Joe reported that Shift+drag in the cc-dashboard selected text across all four
panes, not within a single pane. This contradicted the claim made when shipping
cc-dashboard that "Shift+click/drag selects within the focused pane only."

## Observation

Shift+drag in a terminal emulator operates at the terminal level. tmux renders
all panes into a single terminal screen; pane borders are drawn characters, not
real widget boundaries. The terminal has no knowledge of tmux's logical pane
layout, so any click-drag selection spans the entire rendered screen regardless
of which pane is focused or whether Shift is held.

The only pane-scoped selection mechanism tmux offers is copy mode (`prefix + [`),
which is keyboard-driven.

## Generalization

tmux `mouse on` is useful for pane focus and scroll-wheel navigation, but it
cannot deliver pane-isolated text selection. Any claim that "Shift+drag selects
within the focused pane" is incorrect. When per-pane text selection is a stated
requirement, tmux is the wrong tool; a tiling terminal emulator (Tilix, iTerm2
splits, GNOME Terminal splits) that renders each pane as a real widget is the
correct one.

## Proposed rule

When recommending tmux mouse mode, do not claim it supports pane-scoped text
selection. State the limitation upfront and name the alternative (copy mode or
a tiling terminal).

## Proposed remediation

none — this is a documentation/claim accuracy issue, not a code defect.

## Notes

The `cc-dashboard` script is still useful for focus and scrolling. The
limitation affects only rubber-band text selection across pane content.
