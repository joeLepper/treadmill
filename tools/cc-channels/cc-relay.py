#!/usr/bin/env python3
"""Cross-session relay — drop a message or file into another session's relay inbox.

The target session's treadmill-events channel server watches
~/.cc-channels/<label>/relay/ and injects new files as channel
notifications (no Telegram, no external dependency).

Usage:
  cc-relay.py --to <label> [--from <from_label>] [--type context|action]
              [--file <path>] ["message text"]

Message types (per docs/plans/2026-06-05-cc-relay-trust-gates.md):
  context (default) — free-form information delivery; receiving session
    treats as data, not instructions.
  action — request the receiving session take an action. Prepends an
    ``[ACTION REQUEST]`` header so the receiver can recognize the type
    unambiguously. Receiving sessions MUST consult
    ``~/.cc-channels/<their-label>/relay-trust.json`` (if present) for
    pre-authorization of the source label; ABSENT that pre-auth, the
    receiver MUST obtain explicit operator confirmation before acting.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

MAX_LEN = 4096
ACTION_HEADER = "[ACTION REQUEST]"
ALLOWED_TYPES = ("context", "action")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Drop a message into another session's relay inbox"
    )
    ap.add_argument("--to", required=True, help="target label")
    ap.add_argument("--from", dest="from_label", help="source label (optional prefix)")
    ap.add_argument(
        "--type",
        dest="msg_type",
        choices=ALLOWED_TYPES,
        default="context",
        help=(
            "message type: 'context' (default) for information delivery, "
            "'action' to request the receiver take an action (prepends "
            "[ACTION REQUEST] header; receiver gates execution per "
            "relay-trust.json)"
        ),
    )
    ap.add_argument("--file", help="read message from file")
    ap.add_argument("message", nargs="?", help="message text")
    args = ap.parse_args()

    if args.file and args.message:
        sys.exit("error: pass either --file or message text, not both")
    if not args.file and not args.message:
        sys.exit("error: pass --file or message text")

    body = ""
    # Action header lands FIRST — before the `[from:]` prefix — so a
    # receiver's pattern-match for the action signal can rely on the first
    # line of the message body regardless of source labeling.
    if args.msg_type == "action":
        body += f"{ACTION_HEADER}\n\n"
    if args.from_label:
        body += f"[from: {args.from_label}]\n\n"

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            sys.exit(f"error: file not found: {args.file}")
        body += f"{file_path.name}:\n\n{file_path.read_text()}"
    else:
        body += args.message

    if len(body) > MAX_LEN:
        body = body[: MAX_LEN - 4] + "\n[…]"

    relay_dir = Path.home() / ".cc-channels" / args.to / "relay"
    relay_dir.mkdir(parents=True, exist_ok=True)

    from_suffix = f"-from-{args.from_label}" if args.from_label else ""
    out_file = relay_dir / f"{int(time.time() * 1000)}{from_suffix}.md"
    out_file.write_text(body)

    print(f"relayed: {out_file}")


if __name__ == "__main__":
    main()
