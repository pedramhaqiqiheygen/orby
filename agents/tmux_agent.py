"""tmux session agent backend.

Bridges an existing Claude Code CLI session running in tmux to Slack.
Injects messages via `tmux send-keys`, captures output via `capture-pane`.
Responses come asynchronously via Claude Code hooks (see hooks/notify.py).

Modeled after Claude-Code-Remote's TmuxInjector pattern.
"""

import asyncio
import logging
import shutil

from core.agent import Agent, QueryResult, OnText, OnTool
from core.session import SessionManager

log = logging.getLogger("orby.agents.tmux")


class TmuxAgent(Agent):
    """Agent that injects into and captures from tmux Claude sessions."""

    def __init__(self, session_mgr: SessionManager):
        self.session_mgr = session_mgr

    async def send(self, session_key: str, prompt: str,
                   on_text: OnText | None = None,
                   on_tool: OnTool | None = None) -> QueryResult:
        session = self.session_mgr.get(session_key)
        if not session or session.get("agent_type") != "tmux":
            return QueryResult(text="No tmux session attached.", is_error=True)

        tmux_name = session["tmux_session"]
        if not await self._session_exists(tmux_name):
            return QueryResult(
                text=f"tmux session `{tmux_name}` no longer exists. Use /detach.",
                is_error=True,
            )

        # 3-step injection with delays (matching Claude-Code-Remote pattern)
        # Step 1: Clear any existing input
        await self._keys(tmux_name, "C-u")
        await asyncio.sleep(0.2)
        # Step 2: Type the command (shell-escaped)
        escaped = prompt.replace("'", "'\"'\"'")
        await self._keys(tmux_name, "-l", escaped)
        await asyncio.sleep(0.2)
        # Step 3: Press Enter (C-m = carriage return, more reliable than Enter)
        await self._keys(tmux_name, "C-m")

        # Response comes via hooks, not from this method
        self.session_mgr.set(session_key, session)  # Update last_used
        return QueryResult(text=f"Sent to `{tmux_name}`.")

    async def interrupt(self, session_key: str) -> bool:
        tmux_name = self._get_tmux_name(session_key)
        if not tmux_name:
            return False
        await self._keys(tmux_name, "C-c")
        return True

    async def approve(self, session_key: str) -> bool:
        tmux_name = self._get_tmux_name(session_key)
        if not tmux_name:
            return False
        await self._keys(tmux_name, "y")
        await self._keys(tmux_name, "Enter")
        return True

    async def reject(self, session_key: str) -> bool:
        tmux_name = self._get_tmux_name(session_key)
        if not tmux_name:
            return False
        await self._keys(tmux_name, "n")
        await self._keys(tmux_name, "Enter")
        return True

    async def get_screen(self, session_key: str) -> str:
        """Capture the current tmux pane content."""
        tmux_name = self._get_tmux_name(session_key)
        if not tmux_name:
            return "No tmux session attached."
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-t", tmux_name, "-p", "-S", "-50",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().rstrip()

    # -- Helpers ---------------------------------------------------------------

    def _get_tmux_name(self, session_key: str) -> str | None:
        session = self.session_mgr.get(session_key)
        if session and session.get("agent_type") == "tmux":
            return session["tmux_session"]
        return None

    @staticmethod
    async def _keys(tmux_session: str, *args: str):
        """Run tmux send-keys with given args."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", tmux_session, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    @staticmethod
    async def _session_exists(tmux_session: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", tmux_session,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    @staticmethod
    async def list_sessions() -> list[str]:
        """List available tmux sessions."""
        if not shutil.which("tmux"):
            return []
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-sessions", "-F", "#{session_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        return [s.strip() for s in stdout.decode().splitlines() if s.strip()]
