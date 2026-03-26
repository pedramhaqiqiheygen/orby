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
import shutil
import sys
import time
from pathlib import Path

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

# Track running SDK tasks so they can be cancelled via !kill
_running_tasks: dict[str, asyncio.Task] = {}  # session_key -> asyncio.Task
_session_uploads: dict[str, list[str]] = {}  # session_key -> [file_paths]


# -- Helpers ------------------------------------------------------------------

def parse_message(text: str) -> str:
    """Strip bot mention from message text."""
    return re.sub(r"<@[\w]+>", "", text).strip()


def _cleanup_uploads(max_age_hours: int = 24):
    """Delete upload files older than max_age_hours."""
    upload_dir = ORBY_DIR / "uploads"
    if not upload_dir.is_dir():
        return
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for session_dir in upload_dir.iterdir():
        if not session_dir.is_dir():
            continue
        for f in session_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        # Remove empty session dirs
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            session_dir.rmdir()
    if removed:
        log.info("Cleaned up %d old uploads", removed)


def _cleanup_session_uploads(session_key: str):
    """Delete all uploads for a specific session."""
    _session_uploads.pop(session_key, None)
    safe_key = session_key.replace("/", "_")
    session_dir = ORBY_DIR / "uploads" / safe_key
    if session_dir.is_dir():
        shutil.rmtree(session_dir, ignore_errors=True)
        log.info("Cleaned up uploads for session %s", session_key)


async def _download_files(files: list[dict], session_key: str) -> list[str]:
    """Download Slack files to ~/.orby/uploads/<session>/, return local paths."""
    import aiohttp

    safe_key = session_key.replace("/", "_")
    upload_dir = ORBY_DIR / "uploads" / safe_key
    upload_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    for f in files:
        url = f.get("url_private_download")
        if not url:
            continue
        name = f.get("name", "file")
        dest = upload_dir / f"{int(time.time())}_{name}"

        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {cfg['slack_bot_token']}"}
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    dest.write_bytes(await resp.read())
                    paths.append(str(dest))
                    log.info("Downloaded file: %s (%d bytes)", dest.name, f.get("size", 0))
                else:
                    log.warning("Failed to download %s: HTTP %d", name, resp.status)

    _session_uploads.setdefault(session_key, []).extend(paths)
    return paths


# -- Event handlers -----------------------------------------------------------

@app.event("app_mention")
async def handle_mention(event, client, say):
    await _handle(event, client)


@app.event("message")
async def handle_dm(event, client, say):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id"):
        return
    if event.get("subtype") and event.get("subtype") != "file_share":
        return
    await _handle(event, client)


@app.event("reaction_added")
async def handle_reaction(event, client, say):
    emoji = event.get("reaction")
    if emoji not in ("white_check_mark", "unlock", "x"):
        return

    # Ignore bot's own reactions
    bot_info = await client.auth_test()
    if event.get("user") == bot_info.get("user_id"):
        return

    item = event.get("item", {})
    channel = item.get("channel")
    msg_ts = item.get("ts")
    if not channel or not msg_ts:
        return

    # Find the thread this message belongs to
    result = await client.conversations_history(
        channel=channel, latest=msg_ts, inclusive=True, limit=1)
    msgs = result.get("messages", [])
    if not msgs:
        return
    thread_ts = msgs[0].get("thread_ts", msg_ts)

    session_key = SessionManager.make_key(channel, thread_ts)
    session = session_mgr.get(session_key)
    if not session or session.get("agent_type") != "tmux":
        return

    if emoji == "white_check_mark":
        await tmux_agent.approve(session_key)
        log.info("Reaction approve (once) for %s", session_key)
    elif emoji == "unlock":
        await tmux_agent.allow_session(session_key)
        log.info("Reaction approve (session) for %s", session_key)
    elif emoji == "x":
        await tmux_agent.reject(session_key)
        log.info("Reaction reject for %s", session_key)


async def _handle(event: dict, client):
    channel = event["channel"]
    user = event.get("user", "unknown")
    thread_ts = event.get("thread_ts") or event["ts"]
    prompt = parse_message(event.get("text", ""))
    session_key = SessionManager.make_key(channel, thread_ts)

    # Handle file uploads — download and prepend paths to prompt
    files = event.get("files", [])
    if files:
        downloaded = await _download_files(files, session_key)
        if downloaded:
            file_refs = "\n".join(f"[Uploaded file: {path}]" for path in downloaded)
            prompt = f"{file_refs}\n\n{prompt}" if prompt else file_refs

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

    if prompt == "!kill":
        await _cmd_kill(client, channel, thread_ts, session_key)
        return

    if prompt.lower().startswith("!create "):
        await _cmd_create(client, channel, thread_ts, session_key, prompt[8:].strip())
        return

    if prompt == "!cleanup":
        _cleanup_uploads(max_age_hours=0)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":broom: All uploads cleaned up.",
        )
        return

    if prompt.lower() in ("!interrupt", "!stop"):
        # Remap to the handler below (strip the !)
        prompt = prompt[1:]

    # -- Agent-specific quick commands (tmux mode) -----------------------------

    session = session_mgr.get(session_key)
    is_tmux = session and session.get("agent_type") == "tmux"

    # Permission prompt responses — send the exact keystroke to tmux
    # Claude Code's prompts use arrow keys + Enter, so we send the option number
    # which navigates to that item, then Enter to select it
    if is_tmux and prompt.lower() in ("approve", "y", "yes"):
        ok = await tmux_agent.approve(session_key)
        emoji = ":white_check_mark:" if ok else ":x:"
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"{emoji} Approved")
        return

    if is_tmux and prompt.lower() in ("reject", "n", "no"):
        ok = await tmux_agent.reject(session_key)
        emoji = ":white_check_mark:" if ok else ":x:"
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"{emoji} Denied")
        return

    if is_tmux and prompt.strip() in ("1", "2", "3", "4"):
        # Numbered option — navigate to it in Claude's selector
        # Option 1 is default (top), 2 = one down, 3 = two down, etc.
        option_num = int(prompt.strip())
        tmux_name = session.get("tmux_session")
        if tmux_name:
            # First go to top, then arrow down to the right option
            await tmux_agent._keys(tmux_name, "Home")
            await asyncio.sleep(0.1)
            for _ in range(option_num - 1):
                await tmux_agent._keys(tmux_name, "Down")
                await asyncio.sleep(0.1)
            await tmux_agent._keys(tmux_name, "Enter")
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":white_check_mark: Selected option {option_num}",
            )
        return

    if is_tmux and prompt.lower() in ("interrupt", "stop", "!interrupt", "!stop", "esc"):
        tmux_name = session.get("tmux_session")
        if tmux_name:
            # Send Escape to cancel current action, then Ctrl+C as backup
            await tmux_agent._keys(tmux_name, "Escape")
            await asyncio.sleep(0.2)
            await tmux_agent._keys(tmux_name, "Escape")
            await asyncio.sleep(0.2)
            await tmux_agent._keys(tmux_name, "C-c")
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":white_check_mark: Interrupted (sent Esc + Ctrl+C)",
            )
        return

    # -- Route to agent --------------------------------------------------------

    if is_tmux:
        await _handle_tmux(client, channel, thread_ts, session_key, prompt)
    else:
        # Run SDK handler as a tracked task so !kill can cancel it
        task = asyncio.create_task(
            _handle_sdk(client, channel, thread_ts, session_key, prompt)
        )
        _running_tasks[session_key] = task
        try:
            await task
        except asyncio.CancelledError:
            log.info("SDK task cancelled for %s", session_key)
        finally:
            _running_tasks.pop(session_key, None)


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


async def _cmd_kill(client, channel, thread_ts, session_key):
    """Kill the session attached to this thread (tmux or SDK)."""
    session = session_mgr.get(session_key)

    if not session:
        # Check if there's a running SDK task even without a stored session
        task = _running_tasks.pop(session_key, None)
        if task and not task.done():
            task.cancel()
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":skull: Cancelled running task.",
            )
            return
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="No session found for this thread.",
        )
        return

    agent_type = session.get("agent_type", "sdk")

    if agent_type == "tmux":
        tmux_name = session.get("tmux_session")
        if tmux_name:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "kill-session", "-t", tmux_name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            session_mgr.delete(session_key)
            _cleanup_session_uploads(session_key)
            if proc.returncode == 0:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f":skull: Killed tmux session `{tmux_name}`.",
                )
            else:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f":x: tmux session `{tmux_name}` not found (may already be dead).",
                )
    else:
        # SDK mode — cancel the asyncio task
        task = _running_tasks.pop(session_key, None)
        if task and not task.done():
            task.cancel()
        session_mgr.delete(session_key)
        _cleanup_session_uploads(session_key)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":skull: Killed SDK session.",
        )


async def _cmd_create(client, channel, thread_ts, session_key, name):
    """Create a new Claude Code session in a named tmux session and attach this thread."""
    if not name:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Usage: `!create <session-name>`",
        )
        return

    # Check if tmux session already exists
    if await TmuxAgent._session_exists(name):
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":warning: tmux session `{name}` already exists. Use `!attach {name}` instead.",
        )
        return

    # Create tmux session running claude in the configured default cwd
    work_dir = cfg["default_cwd"]
    proc = await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", name,
        "-c", work_dir,
        "claude", "--permission-mode", cfg["permission_mode"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    if proc.returncode != 0:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":x: Failed to create tmux session `{name}`.",
        )
        return

    # Wait for Claude to start up
    await asyncio.sleep(3)

    # Auto-accept workspace trust prompt
    for _ in range(3):
        await asyncio.sleep(1)
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", name, "Enter",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    # Attach this thread to the new session
    session_mgr.set(session_key, {
        "agent_type": "tmux",
        "tmux_session": name,
    })
    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":rocket: Created Claude Code session `{name}`\n"
             f"*Working dir*: `{work_dir}`\n\n"
             f"Messages in this thread go directly to Claude.\n"
             f"Commands: `!interrupt`, `!screen`, `!kill`, `!detach`",
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
    _cleanup_uploads(max_age_hours=24)  # clean stale uploads on startup
    handler = AsyncSocketModeHandler(app, cfg["slack_app_token"])
    log.info("Orby is online.")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
