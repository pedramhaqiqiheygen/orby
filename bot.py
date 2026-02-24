#!/usr/bin/env python3
"""Orby: Extensible Agent-Channel Bridge.

Routes Slack messages to the correct agent backend:
  - SDK Mode (default): Claude Agent SDK programmatic sessions
  - Attach Mode: Bridge to an existing tmux Claude Code CLI session

Commands:
  !attach <session>  - Attach to a tmux session or Claude session ID
  !detach            - Detach from tmux session, revert to SDK mode
  !screen            - Show current tmux pane content
  !status            - Show session info
  !sessions          - List available tmux sessions
  approve / y        - Approve a pending permission prompt (tmux mode)
  reject / n         - Reject a pending permission prompt (tmux mode)
  interrupt / stop   - Interrupt Claude (tmux mode)
"""

import asyncio
import logging
import re
import sys
import time

from slack_bolt.app.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from config import load_config, ORBY_DIR
from core.session import SessionManager
from agents.sdk_agent import SDKAgent
from agents.tmux_agent import TmuxAgent
from formatter import split_message

# -- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ORBY_DIR / "orby.log"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("orby")

# -- App setup ----------------------------------------------------------------

cfg = load_config()
app = AsyncApp(token=cfg["slack_bot_token"])

session_mgr = SessionManager()
sdk_agent = SDKAgent(
    session_mgr=session_mgr,
    default_cwd=cfg["default_cwd"],
    permission_mode=cfg["permission_mode"],
    max_turns=cfg["max_turns"],
    allowed_tools=cfg["allowed_tools"],
)
tmux_agent = TmuxAgent(session_mgr=session_mgr)


# -- Helpers ------------------------------------------------------------------

def parse_message(text: str) -> str:
    """Strip bot mention from message text."""
    return re.sub(r"<@[\w]+>", "", text).strip()


# -- Event handlers -----------------------------------------------------------

@app.event("app_mention")
async def handle_mention(event, client, say):
    await _handle(event, client)


@app.event("message")
async def handle_dm(event, client, say):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    await _handle(event, client)


async def _handle(event: dict, client):
    channel = event["channel"]
    user = event.get("user", "unknown")
    thread_ts = event.get("thread_ts") or event["ts"]
    prompt = parse_message(event.get("text", ""))
    session_key = SessionManager.make_key(channel, thread_ts)

    if not prompt:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Send me a message and I'll route it to Claude Code.",
        )
        return

    log.info("Message from %s: %s", user, prompt[:100])

    # -- Command routing -------------------------------------------------------

    if prompt.startswith("!attach "):
        await _cmd_attach(client, channel, thread_ts, session_key, prompt[8:].strip())
        return

    if prompt == "!detach":
        await _cmd_detach(client, channel, thread_ts, session_key)
        return

    if prompt == "!screen":
        await _cmd_screen(client, channel, thread_ts, session_key)
        return

    if prompt == "!status":
        await _cmd_status(client, channel, thread_ts, session_key)
        return

    if prompt == "!sessions":
        await _cmd_list_sessions(client, channel, thread_ts)
        return

    # -- Agent-specific quick commands (tmux mode) -----------------------------

    session = session_mgr.get(session_key)
    is_tmux = session and session.get("agent_type") == "tmux"

    if is_tmux and prompt.lower() in ("approve", "y", "yes"):
        ok = await tmux_agent.approve(session_key)
        emoji = ":white_check_mark:" if ok else ":x:"
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"{emoji} Approved")
        return

    if is_tmux and prompt.lower() in ("reject", "n", "no"):
        ok = await tmux_agent.reject(session_key)
        emoji = ":white_check_mark:" if ok else ":x:"
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"{emoji} Rejected")
        return

    if is_tmux and prompt.lower() in ("interrupt", "stop"):
        ok = await tmux_agent.interrupt(session_key)
        emoji = ":white_check_mark:" if ok else ":x:"
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"{emoji} Interrupted")
        return

    # -- Route to agent --------------------------------------------------------

    if is_tmux:
        await _handle_tmux(client, channel, thread_ts, session_key, prompt)
    else:
        await _handle_sdk(client, channel, thread_ts, session_key, prompt)


# -- Command implementations --------------------------------------------------

def _is_session_id(s: str) -> bool:
    """Check if a string is a UUID (Claude session ID)."""
    import re
    return bool(re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', s, re.I))


def _find_session_cwd(session_id: str) -> str | None:
    """Find the working directory for a Claude session."""
    from pathlib import Path
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if (project_dir / f"{session_id}.jsonl").exists():
            decoded = "/" + project_dir.name.lstrip("-").replace("-", "/")
            if Path(decoded).is_dir():
                return decoded
    return None


async def _cmd_attach(client, channel, thread_ts, session_key, target):
    """Attach to a tmux session by name, or launch one from a Claude session ID.

    /attach my-tmux-session          → attach to existing tmux session
    /attach 667468c4-9cbc-4f59-...   → find session, start claude --resume in new tmux
    """
    if _is_session_id(target):
        await _attach_by_session_id(client, channel, thread_ts, session_key, target)
    else:
        await _attach_by_tmux_name(client, channel, thread_ts, session_key, target)


async def _attach_by_tmux_name(client, channel, thread_ts, session_key, tmux_name):
    """Attach to an already-running tmux session."""
    if not await TmuxAgent._session_exists(tmux_name):
        available = await TmuxAgent.list_sessions()
        msg = f":x: tmux session `{tmux_name}` not found."
        if available:
            msg += f"\n\nAvailable sessions:\n" + "\n".join(f"  `{s}`" for s in available)
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)
        return

    session_mgr.set(session_key, {
        "agent_type": "tmux",
        "tmux_session": tmux_name,
    })
    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":link: Attached to `{tmux_name}`.\n\n"
             f"Messages in this thread go directly to Claude.\n"
             f"Commands: `approve`, `reject`, `interrupt`, `!screen`, `!detach`",
    )


async def _attach_by_session_id(client, channel, thread_ts, session_key, target):
    """Launch claude --resume <id> in a new tmux session and attach to it."""
    import asyncio

    cwd = _find_session_cwd(target)
    if not cwd:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":x: Session `{target[:16]}...` not found in `~/.claude/projects/`.",
        )
        return

    session_id = target

    # Generate a tmux session name from the session ID
    tmux_name = f"orby-{session_id[:8]}"

    # Kill existing tmux session with this name if any
    proc = await asyncio.create_subprocess_exec(
        "tmux", "kill-session", "-t", tmux_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Launch claude --resume in a new tmux session
    proc = await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", tmux_name, "-c", cwd,
        "claude", "--resume", session_id, "--dangerously-skip-permissions",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    if proc.returncode != 0:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":x: Failed to start tmux session `{tmux_name}`.",
        )
        return

    # Auto-approve the workspace trust prompt and session picker
    # Claude shows: 1) trust dialog, 2) session picker on --resume
    # Send Enter to accept trust, then wait, then Enter to accept session
    for _ in range(5):
        await asyncio.sleep(1.5)
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", tmux_name, "Enter",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    await asyncio.sleep(2)

    # Clear claude_session_id from any old sessions to avoid stale matches
    session_mgr.clear_claude_session_id(session_id)
    session_mgr.set(session_key, {
        "agent_type": "tmux",
        "tmux_session": tmux_name,
        "claude_session_id": session_id,
    })
    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":link: Resumed `{session_id[:16]}...` in tmux session `{tmux_name}`\n"
             f"*Working dir*: `{cwd}`\n\n"
             f"Messages in this thread go directly to Claude.\n"
             f"Commands: `approve`, `reject`, `interrupt`, `!screen`, `!detach`",
    )


async def _cmd_detach(client, channel, thread_ts, session_key):
    session = session_mgr.get(session_key)
    if session and session.get("agent_type") == "tmux":
        session_mgr.delete(session_key)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":broken_chain: Detached. New messages will create SDK sessions.",
        )
    else:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Not attached to a tmux session.",
        )


async def _cmd_screen(client, channel, thread_ts, session_key):
    screen = await tmux_agent.get_screen(session_key)
    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"```\n{screen[:3800]}\n```",
    )


async def _cmd_status(client, channel, thread_ts, session_key):
    session = session_mgr.get(session_key)
    if not session:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="No active session for this thread. Send a message to start one.",
        )
        return

    agent_type = session.get("agent_type", "sdk")
    lines = [f"*Agent*: {agent_type}"]
    if agent_type == "tmux":
        lines.append(f"*tmux session*: `{session.get('tmux_session', '?')}`")
    elif agent_type == "sdk":
        lines.append(f"*Session ID*: `{session.get('agent_session_id', '?')[:16]}...`")
        lines.append(f"*Working dir*: `{session.get('cwd', '?')}`")
    lines.append(f"*Last used*: {session.get('last_used', '?')}")

    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text="\n".join(lines),
    )


async def _cmd_list_sessions(client, channel, thread_ts):
    sessions = await TmuxAgent.list_sessions()
    if sessions:
        lines = ["*Available tmux sessions:*"] + [f"  `{s}`" for s in sessions]
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines))
    else:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="No tmux sessions found.")


# -- Agent handlers ------------------------------------------------------------

async def _handle_tmux(client, channel, thread_ts, session_key, prompt):
    """Route message to tmux agent (fire-and-forget, response comes via hooks)."""
    result = await tmux_agent.send(session_key, prompt)
    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":arrow_right: {result.text}",
    )


async def _handle_sdk(client, channel, thread_ts, session_key, prompt):
    """Route message to SDK agent with progressive Slack updates."""
    # Parse optional /cd directive
    cwd = None
    if prompt.startswith("/cd "):
        parts = prompt.split("\n", 1)
        cwd = parts[0][4:].strip()
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"Working directory set to `{cwd}`. Send your next message.",
            )
            # Update session cwd
            session = session_mgr.get(session_key) or {}
            session["cwd"] = cwd
            session.setdefault("agent_type", "sdk")
            session_mgr.set(session_key, session)
            return

    # Post placeholder
    resp = await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Thinking...",
    )
    placeholder_ts = resp["ts"]
    last_update = [0.0]

    async def on_text(accumulated: str):
        now = time.time()
        if now - last_update[0] >= 1.5:
            last_update[0] = now
            try:
                await client.chat_update(
                    channel=channel, ts=placeholder_ts, text=accumulated[:3800],
                )
            except Exception:
                pass

    async def on_tool(summary: str):
        try:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=f"_{summary}_",
            )
        except Exception:
            pass

    try:
        result = await sdk_agent.send(session_key, prompt, on_text=on_text, on_tool=on_tool)

        full_text = result.text or "_No response from Claude._"
        cost_line = f"\n\n_${result.cost:.4f}_" if result.cost else ""
        chunks = split_message(full_text)

        await client.chat_update(
            channel=channel, ts=placeholder_ts,
            text=chunks[0] + (cost_line if len(chunks) == 1 else ""),
        )
        for i, chunk in enumerate(chunks[1:], 1):
            suffix = cost_line if i == len(chunks) - 1 else ""
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk + suffix,
            )

    except Exception as e:
        log.exception("Error in SDK query")
        await client.chat_update(
            channel=channel, ts=placeholder_ts,
            text=f":x: Error: {type(e).__name__}: {e}",
        )


# -- Main ---------------------------------------------------------------------

async def main():
    handler = AsyncSocketModeHandler(app, cfg["slack_app_token"])
    log.info("Orby is online.")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
