"""Claude Agent SDK backend.

Creates and resumes Claude Code sessions programmatically via the Agent SDK.
Each Slack thread maps to one SDK session with full conversation history.
"""

import logging
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from core.agent import Agent, QueryResult, OnText, OnTool
from core.session import SessionManager
from formatter import format_tool_use

log = logging.getLogger("orby.agents.sdk")


class SDKAgent(Agent):
    """Agent backed by Claude Agent SDK (programmatic sessions)."""

    def __init__(self, session_mgr: SessionManager, default_cwd: str,
                 permission_mode: str, max_turns: int, allowed_tools: list[str]):
        self.session_mgr = session_mgr
        self.default_cwd = default_cwd
        self.permission_mode = permission_mode
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools

    async def send(self, session_key: str, prompt: str,
                   on_text: OnText | None = None,
                   on_tool: OnTool | None = None) -> QueryResult:
        session = self.session_mgr.get(session_key)
        work_dir = (session or {}).get("cwd") or self.default_cwd

        opts = ClaudeAgentOptions(
            cwd=work_dir,
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            max_turns=self.max_turns,
        )
        agent_sid = (session or {}).get("agent_session_id")
        if agent_sid:
            opts.resume = agent_sid

        result = QueryResult()
        text_parts: list[str] = []

        # Prevent "cannot launch inside another Claude Code session" error
        # when Orby itself is running inside a Claude Code session
        os.environ.pop("CLAUDECODE", None)

        try:
            async for msg in query(prompt=prompt, options=opts):
                if msg is None:
                    continue

                # Extract session_id from SystemMessage.data or ResultMessage
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
            # Give a clear message for common errors
            if "exit code 1" in err_msg and agent_sid:
                log.error("Failed to resume session %s in %s", agent_sid[:16], work_dir)
                result.is_error = True
                result.text = (
                    f"Failed to resume session `{agent_sid[:16]}...` "
                    f"in `{work_dir}`. The session may have expired or "
                    f"the working directory may be wrong."
                )
                return result
            log.exception("Error during SDK query")
            raise

        # Persist session
        if result.session_id:
            self.session_mgr.set(session_key, {
                "agent_type": "sdk",
                "agent_session_id": result.session_id,
                "cwd": work_dir,
            })

        result.text = "".join(text_parts)
        return result

    async def interrupt(self, session_key: str) -> bool:
        # Not supported with the functional query() API
        return False

    async def approve(self, session_key: str) -> bool:
        # Not applicable - permission_mode handles this automatically
        return False

    async def reject(self, session_key: str) -> bool:
        return False
