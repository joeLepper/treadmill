#!/usr/bin/env python3
"""Cross-session Telegram message relay (ADR-0067/0068).

Send a message or file to another labeled session's Telegram channel.
Resolves the target session's Telegram credentials from its persistent
state dir.

Usage:
  cc-relay.py --to <label> [--from <from_label>] [--file <path>]
              ["message text"]

The target's TELEGRAM_CHAT_ID must be configured in
~/.cc-channels/<label>/telegram.env (one-time setup per label).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse bash-style env file: KEY=VALUE, skip blanks and comments."""
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return env


def send_message(chat_id: str, text: str, token: str) -> str:
    """Send message via Telegram API. Returns message_id on success; exits on error."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            if not (200 <= resp.status < 300):
                sys.exit(f"error: HTTP {resp.status}: {resp.read().decode()}")
            response = json.loads(resp.read().decode())
            return str(response["result"]["message_id"])
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code}: {e.read().decode()}")
    except Exception as e:
        sys.exit(f"error: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Send a message or file to another session's Telegram channel"
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

    # Load target's env file
    env_path = Path.home() / ".cc-channels" / args.to / "telegram.env"
    if not env_path.exists():
        sys.exit(
            f"error: ~/.cc-channels/{args.to}/telegram.env not found"
        )

    env = parse_env_file(env_path)
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")

    if not token:
        sys.exit(
            f"error: TELEGRAM_BOT_TOKEN not set in ~/.cc-channels/{args.to}/telegram.env"
        )
    if not chat_id:
        sys.exit(
            f"error: TELEGRAM_CHAT_ID not set in ~/.cc-channels/{args.to}/telegram.env — add it (one-time setup)"
        )

    # Build message text
    text = ""

    if args.from_label:
        text += f"[from: {args.from_label}]\n\n"

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            sys.exit(f"error: file not found: {args.file}")
        file_text = file_path.read_text()
        filename = file_path.name
        text += f"📄 {filename}:\n\n{file_text}"
    else:
        text += args.message

    # Truncate to 4096 chars
    max_len = 4096
    if len(text) > max_len:
        text = text[: max_len - 4] + "\n[…]"

    # Send
    message_id = send_message(chat_id, text, token)
    print(f"sent: message_id={message_id}")


if __name__ == "__main__":
    main()
