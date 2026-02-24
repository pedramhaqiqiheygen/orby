"""Abstract Agent base class.

All agent backends (Claude SDK, tmux, future agents) implement this interface.
The router in bot.py dispatches to the correct agent based on session type.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Awaitable

# Callback types for progressive streaming
OnText = Callable[[str], Awaitable[None]]
OnTool = Callable[[str], Awaitable[None]]


@dataclass
class QueryResult:
    """Result returned by any agent's send() method."""
    text: str = ""
    session_id: str | None = None
    cost: float | None = None
    num_turns: int = 0
    is_error: bool = False
    tool_summaries: list[str] = field(default_factory=list)


class Agent(ABC):
    """Base class for AI agent backends.

    Subclasses implement the interaction pattern for a specific backend:
    - SDKAgent: Programmatic Claude Agent SDK sessions
    - TmuxAgent: Inject/capture from a running tmux CLI session
    """

    @abstractmethod
    async def send(
        self,
        session_key: str,
        prompt: str,
        on_text: OnText | None = None,
        on_tool: OnTool | None = None,
    ) -> QueryResult:
        """Send a prompt to the agent and return the result.

        For SDK agents, this runs a full query with streaming callbacks.
        For tmux agents, this injects text and returns immediately
        (responses come via hooks).
        """

    @abstractmethod
    async def interrupt(self, session_key: str) -> bool:
        """Interrupt the current task. Returns True if successful."""

    @abstractmethod
    async def approve(self, session_key: str) -> bool:
        """Approve a pending permission request."""

    @abstractmethod
    async def reject(self, session_key: str) -> bool:
        """Reject a pending permission request."""
