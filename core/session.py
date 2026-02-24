"""Session persistence layer.

Maps Slack threads to agent sessions. Each thread can be bound to either
an SDK session (agent_type="sdk") or a tmux session (agent_type="tmux").
Sessions are stored in ~/.orby/sessions.json with atomic writes.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import ORBY_DIR

log = logging.getLogger("orby.session")

SESSIONS_FILE = ORBY_DIR / "sessions.json"
MAX_SESSION_AGE_DAYS = 7


class SessionManager:
    """Thread-to-session mapping with file persistence."""

    def __init__(self):
        self.sessions = self._prune(_load(SESSIONS_FILE))
        _save(SESSIONS_FILE, self.sessions)

    @staticmethod
    def make_key(channel: str, thread_ts: str) -> str:
        return f"{channel}_{thread_ts}"

    def get(self, key: str) -> dict | None:
        return self.sessions.get(key)

    def set(self, key: str, data: dict):
        data["last_used"] = datetime.utcnow().isoformat()
        if key not in self.sessions:
            data.setdefault("created", datetime.utcnow().isoformat())
        self.sessions[key] = data
        _save(SESSIONS_FILE, self.sessions)
        log.info("Session %s: %s", "created" if "created" in data else "updated", key)

    def delete(self, key: str) -> bool:
        if key in self.sessions:
            del self.sessions[key]
            _save(SESSIONS_FILE, self.sessions)
            log.info("Session deleted: %s", key)
            return True
        return False

    def find_by_tmux(self, tmux_session: str) -> tuple[str, dict] | None:
        """Find a session attached to a given tmux session name.

        Returns (session_key, session_data) or None.
        Used by the hooks/notify.py script to route notifications.
        """
        for key, data in self.sessions.items():
            if data.get("agent_type") == "tmux" and data.get("tmux_session") == tmux_session:
                return key, data
        return None

    def clear_claude_session_id(self, claude_session_id: str):
        """Remove claude_session_id from all sessions that have it.

        Called before setting it on a new session to avoid stale matches.
        """
        changed = False
        for data in self.sessions.values():
            if data.get("claude_session_id") == claude_session_id:
                del data["claude_session_id"]
                changed = True
        if changed:
            _save(SESSIONS_FILE, self.sessions)

    def find_by_claude_session_id(self, claude_session_id: str) -> tuple[str, dict] | None:
        """Find a session by Claude Code's internal session ID.

        Used by hooks when TMUX env is not available - the hook data
        contains session_id which we can match against stored sessions.
        Returns (session_key, session_data) or None.
        """
        for key, data in self.sessions.items():
            if data.get("claude_session_id") == claude_session_id:
                return key, data
        return None

    @staticmethod
    def _prune(sessions: dict) -> dict:
        cutoff = (datetime.utcnow() - timedelta(days=MAX_SESSION_AGE_DAYS)).isoformat()
        return {k: v for k, v in sessions.items() if v.get("last_used", "") > cutoff}


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt sessions.json, starting fresh")
            return {}
    return {}


def _save(path: Path, sessions: dict):
    """Atomic write: write to .tmp, then rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2))
    tmp.rename(path)
