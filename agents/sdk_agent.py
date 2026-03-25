"""Claude Agent SDK backend.

Uses persistent ClaudeSDKClient connections — one per Slack thread.
Pre-warms clients on startup so the first message is instant.
"""

import asyncio
import logging
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from core.agent import Agent, QueryResult, OnText, OnTool
from core.session import SessionManager
from formatter import format_tool_use

log = logging.getLogger("orby.agents.sdk")


class SDKAgent(Agent):
    """Agent backed by Claude Agent SDK with persistent connections and pre-warming."""

    def __init__(self, session_mgr: SessionManager, default_cwd: str,
                 permission_mode: str, max_turns: int, allowed_tools: list[str]):
        self.session_mgr = session_mgr
        self.default_cwd = default_cwd
        self.permission_mode = permission_mode
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._warm_pool: list[ClaudeSDKClient] = []
        self._warming = False
        self._create_lock = asyncio.Lock()

    def _build_options(self, work_dir: str) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=work_dir,
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            max_turns=self.max_turns,
            setting_sources=["user", "project", "local"],
            env={"CLAUDE_CODE_STREAM_CLOSE_TIMEOUT": "180000"},
        )

    async def warm(self):
        """Pre-warm a client in the background. Called on bot startup and after each claim."""
        if self._warming:
            return
        self._warming = True
        try:
            os.environ.pop("CLAUDECODE", None)
            # Set on host process so SDK's Python-side timeout reads it too
            os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = "180000"
            opts = self._build_options(self.default_cwd)
            client = ClaudeSDKClient(options=opts)
            log.info("Pre-warming SDK client (cwd=%s)...", self.default_cwd)
            await client.connect()
            self._warm_pool.append(client)
            log.info("Pre-warmed SDK client ready (%d in pool)", len(self._warm_pool))
        except Exception:
            log.exception("Failed to pre-warm SDK client")
        finally:
            self._warming = False

    def _start_warming(self):
        """Kick off a background warm if not already running."""
        if not self._warming and len(self._warm_pool) == 0:
            asyncio.create_task(self.warm())

    async def _get_or_create_client(self, session_key: str, work_dir: str) -> ClaudeSDKClient:
        """Get existing client, claim a pre-warmed one, or create fresh."""
        if session_key in self._clients:
            return self._clients[session_key]

        async with self._create_lock:
            # Re-check after acquiring lock (another coroutine may have created it)
            if session_key in self._clients:
                return self._clients[session_key]

            # Try to claim a pre-warmed client (only if cwd matches default)
            if self._warm_pool and work_dir == self.default_cwd:
                client = self._warm_pool.pop(0)
                self._clients[session_key] = client
                log.info("Claimed pre-warmed client for %s", session_key)
                self._start_warming()  # replenish
                return client

            # Fallback: create fresh (blocks on init)
            os.environ.pop("CLAUDECODE", None)
            os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = "180000"
            opts = self._build_options(work_dir)
            client = ClaudeSDKClient(options=opts)
            log.info("Connecting new SDK client for %s (cwd=%s)", session_key, work_dir)
            await client.connect()
            self._clients[session_key] = client
            log.info("SDK client connected for %s", session_key)
            self._start_warming()  # start warming next one
            return client

    async def send(self, session_key: str, prompt: str,
                   on_text: OnText | None = None,
                   on_tool: OnTool | None = None) -> QueryResult:
        session = self.session_mgr.get(session_key)
        work_dir = (session or {}).get("cwd") or self.default_cwd

        result = QueryResult()
        text_parts: list[str] = []

        try:
            client = await self._get_or_create_client(session_key, work_dir)
            await client.query(prompt)

            async for msg in client.receive_response():
                if msg is None:
                    continue

                sid = getattr(msg, "session_id", None)
                if not sid and isinstance(msg, SystemMessage):
                    sid = (msg.data or {}).get("session_id")
                if sid:
                    result.session_id = sid

                if isinstance(msg, AssistantMessage):
                    if msg.error:
                        log.warning("Claude API error: %s", msg.error)
                        result.is_error = True
                        text_parts.append(f"[Claude error: {msg.error}]")
                        if on_text:
                            await on_text("".join(text_parts))
                        continue

                    for block in getattr(msg, "content", []):
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                            if on_text:
                                await on_text("".join(text_parts))
                        elif isinstance(block, ToolUseBlock):
                            summary = format_tool_use(block.name, block.input)
                            result.tool_summaries.append(summary)
                            if on_tool:
                                await on_tool(summary)

                elif isinstance(msg, ResultMessage):
                    result.cost = getattr(msg, "total_cost_usd", None)
                    result.num_turns = getattr(msg, "num_turns", 0)
                    result.is_error = getattr(msg, "is_error", False)

        except Exception as e:
            err_msg = str(e)
            self._clients.pop(session_key, None)

            if "exit code" in err_msg or "not connected" in err_msg.lower():
                log.error("SDK client error for %s: %s", session_key, err_msg)
                result.is_error = True
                result.text = f"Session error: {err_msg}. Next message will reconnect."
                return result
            log.exception("Error during SDK query")
            raise

        if result.session_id:
            self.session_mgr.set(session_key, {
                "agent_type": "sdk",
                "agent_session_id": result.session_id,
                "cwd": work_dir,
            })

        result.text = "".join(text_parts)
        return result

    async def interrupt(self, session_key: str) -> bool:
        client = self._clients.get(session_key)
        if client:
            try:
                await client.interrupt()
                return True
            except Exception:
                log.exception("Failed to interrupt SDK session %s", session_key)
        return False

    async def disconnect(self, session_key: str):
        """Disconnect and remove a client for a session."""
        client = self._clients.pop(session_key, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                log.warning("Error disconnecting SDK client for %s", session_key)

    async def approve(self, session_key: str) -> bool:
        return False

    async def reject(self, session_key: str) -> bool:
        return False
