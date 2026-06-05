#!/usr/bin/env python3
"""Cross-session relay — drop a message or file into another session's relay inbox.

The target session's treadmill-events channel server watches
~/.cc-channels/<label>/relay/ and injects new files as channel
notifications (no Telegram, no external dependency).

Usage:
  cc-relay.py --to <label> [--from <from_label>] [--file <path>]
              ["message text"]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

MAX_LEN = 4096


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Drop a message into another session's relay inbox"
    )
    ap.add_argument("--to", required=True, help="target label")
    ap.add_argument("--from", dest="from_label", help="source label (optional prefix)")
    ap.add_argument("--file", help="read message from file")
    ap.add_argument("message", nargs="?", help="message text")
    args = ap.parse_args()

    if args.file and args.message:
        sys.exit("error: pass either --file or message text, not both")
    if not args.file and not args.message:
        sys.exit("error: pass --file or message text")

    body = ""
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
