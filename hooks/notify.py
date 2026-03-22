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


def _capture_tmux_pane(tmux_session: str, lines: int = 30) -> str | None:
    """Capture the last N lines of a tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux_session, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.rstrip()
    except Exception:
        pass
    return None


def _extract_permission_details(screen: str) -> dict | None:
    """Extract structured permission prompt details from tmux screen.

    Returns dict with 'tool', 'command', 'reason', 'options' keys, or None.
    """
    if not screen:
        return None
    raw_lines = screen.splitlines()

    # First pass: extract numbered options from raw lines before filtering
    options = []
    for line in raw_lines[-25:]:
        stripped = line.lstrip("❯ ").strip()
        if stripped and len(stripped) > 2 and stripped[0].isdigit() and stripped[1] == ".":
            options.append(stripped)

    # Second pass: filter out decorative lines for content extraction
    skip_patterns = ["───", "Running", "Esc to cancel", "❯", "Tab to amend", "ctrl+e",
                     "Do you want to proceed"]
    cleaned = []
    for line in raw_lines[-25:]:
        stripped = line.strip()
        if not stripped:
            continue
        if any(p in stripped for p in skip_patterns):
            continue
        # Skip numbered options (already extracted above)
        if stripped.lstrip("❯ ") and stripped.lstrip("❯ ")[0:2] in ("1.", "2.", "3.", "4."):
            continue
        cleaned.append(stripped)

    if not cleaned:
        return None

    tool = None
    command_lines = []
    reason = None

    i = 0
    # Find tool type line
    while i < len(cleaned):
        low = cleaned[i].lower()
        if any(t in low for t in ["bash command", "edit file", "write file", "read file",
                                   "bash(", "edit(", "write(", "read("]):
            tool = cleaned[i]
            i += 1
            break
        i += 1

    # Collect command/content lines until we hit the reason
    while i < len(cleaned):
        if any(kw in cleaned[i].lower() for kw in ["require approval", "may modify",
                                                     "will create", "could be",
                                                     "prevent bare", "approval"]):
            reason = cleaned[i]
            break
        command_lines.append(cleaned[i])
        i += 1

    return {
        "tool": tool or "Unknown tool",
        "command": "\n".join(command_lines) if command_lines else None,
        "reason": reason,
        "options": options,
    }


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

    # Detect tmux session early (needed for screen capture later)
    tmux_session = _detect_tmux_session()

    # Primary: match by Claude's session_id from hook data
    claude_sid = hook_data.get("session_id")
    match = None
    if claude_sid:
        match = mgr.find_by_claude_session_id(claude_sid)
        if match:
            _log(f"  matched by claude_session_id: {match[0]}")

    # Fallback: match by TMUX env var
    if not match:
        _log(f"  TMUX fallback, detected: {tmux_session}")
        if tmux_session:
            match = mgr.find_by_tmux(tmux_session)

    if not match:
        _log("  ABORT: no matching session found")
        return

    session_key, session_data = match

    # Get tmux session name from matched session if not detected from env
    if not tmux_session and session_data.get("agent_type") == "tmux":
        tmux_session = session_data.get("tmux_session")

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

    elif args.event == "PermissionRequest":
        tool = hook_data.get("tool_name", "unknown")
        tool_input = hook_data.get("tool_input", {})
        suggestions = hook_data.get("permission_suggestions", [])

        _log(f"  permission_suggestions: {suggestions}")

        # Format the command/file with full detail
        if tool == "Bash":
            cmd = tool_input.get("command", "?")
            detail = f"```$ {cmd[:800]}```"
        elif tool in ("Edit", "Write"):
            detail = f"`{tool_input.get('file_path', '?')}`"
        elif tool == "Read":
            detail = f"`{tool_input.get('file_path', '?')}`"
        else:
            detail = format_tool_use(tool, tool_input)

        # Capture actual options from tmux screen (most reliable)
        options_text = "  *y* — Allow  |  *n* — Deny"
        if tmux_session:
            # Small delay — the permission UI may still be rendering
            import time
            time.sleep(0.3)
            screen = _capture_tmux_pane(tmux_session)
            details = _extract_permission_details(screen) if screen else None
            if details and details.get("options"):
                options_text = "\n".join(f"  *{o}*" for o in details["options"])

        text = (
            f":lock: *{tool}*\n"
            f"{detail}\n\n"
            f"{options_text}"
        )

    elif args.event == "Notification":
        msg = hook_data.get("message", "")
        is_permission = "permission" in msg.lower() if msg else False

        if is_permission:
            # Skip — PermissionRequest hook handles these with better detail
            _log("  skipping permission notification (handled by PermissionRequest)")
            return
        elif msg:
            text = f"_Claude needs your input:_\n{msg[:500]}"
        else:
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
