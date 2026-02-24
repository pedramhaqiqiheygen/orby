#!/usr/bin/env python3
"""Claude Code hook script - forwards activity to Slack.

Called by Claude Code's hook system via ~/.claude/settings.json.
Reads hook JSON from stdin, finds which Slack thread is attached to
the current tmux session, and posts an update.

Usage in hooks config:
  "command": "~/.orby/.venv/bin/python3 ~/.orby/hooks/notify.py --event Stop"

This script is fire-and-forget (async hooks with 5s timeout).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

# Add orby root to path for imports
ORBY_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ORBY_DIR))

from config import load_config
from core.session import SessionManager
from formatter import format_tool_use


def _detect_tmux_session() -> str | None:
    """Detect which tmux session the hook was called from."""
    tmux_env = os.environ.get("TMUX", "")
    if tmux_env:
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{session_name}"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
    return None


def _post_to_slack(bot_token: str, channel: str, thread_ts: str, text: str) -> str:
    """Post a message to a Slack thread using the bot token."""
    if len(text) > 3800:
        text = text[:3800] + "\n\n_...truncated_"
    payload = json.dumps({
        "channel": channel,
        "thread_ts": thread_ts,
        "text": text,
    }).encode()
    req = Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bot_token}",
        },
    )
    try:
        resp = urlopen(req, timeout=4)
        return resp.read().decode()[:200]
    except Exception as e:
        return f"ERROR: {e}"


def _get_last_response(transcript_path: str) -> str | None:
    """Extract the last assistant text response from the session transcript."""
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.exists():
        return None

    # Read JSONL from the end, find the last assistant message with text
    last_text = None
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "assistant":
                # Extract text blocks from content
                parts = []
                for block in msg.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                if parts:
                    last_text = "".join(parts)
    except Exception:
        return None
    return last_text


def _log(msg: str):
    """Append to hook debug log."""
    try:
        with open(ORBY_DIR / "hooks.log", "a") as f:
            f.write(f"{msg}\n")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    args = parser.parse_args()

    _log(f"--- Hook fired: {args.event}")

    # Read hook input from stdin
    try:
        hook_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_data = {}

    _log(f"  hook_data keys: {list(hook_data.keys())}")
    _log(f"  hook session_id: {hook_data.get('session_id', 'NONE')}")

    mgr = SessionManager()

    # Primary: match by Claude's session_id from hook data
    claude_sid = hook_data.get("session_id")
    match = None
    if claude_sid:
        match = mgr.find_by_claude_session_id(claude_sid)
        if match:
            _log(f"  matched by claude_session_id: {match[0]}")

    # Fallback: match by TMUX env var
    if not match:
        tmux_session = _detect_tmux_session()
        _log(f"  TMUX fallback, detected: {tmux_session}")
        if tmux_session:
            match = mgr.find_by_tmux(tmux_session)

    if not match:
        _log("  ABORT: no matching session found")
        return

    session_key, session_data = match

    # Parse channel and thread_ts from session key
    parts = session_key.rsplit("_", 1)
    if len(parts) != 2:
        return
    channel, thread_ts = parts

    # Format the message based on event type
    text = None

    if args.event == "Stop":
        # Use last_assistant_message directly from hook data (most reliable)
        last_msg = hook_data.get("last_assistant_message")
        if last_msg:
            _log(f"  last_assistant_message type: {type(last_msg).__name__}, len: {len(str(last_msg))}")
            # Can be a string or a structured object
            if isinstance(last_msg, str):
                text = last_msg
            elif isinstance(last_msg, dict):
                # Extract text from content blocks
                parts = []
                for block in last_msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                text = "".join(parts) if parts else str(last_msg)
            else:
                text = str(last_msg)
        if not text:
            # Fallback to transcript
            transcript_path = hook_data.get("transcript_path")
            response = _get_last_response(transcript_path)
            text = response or "_Claude finished. Waiting for input._"

    elif args.event == "PreToolUse":
        tool = hook_data.get("tool_name", "?")
        tool_input = hook_data.get("tool_input", {})
        summary = format_tool_use(tool, tool_input)
        text = f"_{summary}_"

    elif args.event == "PostToolUse":
        # Skip - the Stop hook will post the full response
        return

    elif args.event == "Notification":
        text = "_Claude needs your input._"

    if not text:
        _log("  ABORT: no text to send")
        return

    _log(f"  posting to channel={channel} thread={thread_ts} text_len={len(text)}")
    _log(f"  text preview: {text[:100]}")

    # Post to Slack
    try:
        cfg = load_config()
        resp = _post_to_slack(cfg["slack_bot_token"], channel, thread_ts, text)
        _log(f"  slack response: {resp}")
    except Exception as e:
        _log(f"  slack error: {e}")


if __name__ == "__main__":
    main()
