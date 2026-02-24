"""Configuration loader for Orby.

Reads ~/.orby/config.env and provides typed defaults.
Environment variables always take precedence over the file.
"""

import os
from pathlib import Path

ORBY_DIR = Path.home() / ".orby"


def load_config() -> dict:
    """Load config.env key=value pairs and return a config dict."""
    env_file = ORBY_DIR / "config.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    return {
        "slack_bot_token": os.environ["SLACK_BOT_TOKEN"],
        "slack_app_token": os.environ["SLACK_APP_TOKEN"],
        "default_cwd": os.environ.get("ORBY_DEFAULT_CWD", str(Path.home())),
        "permission_mode": os.environ.get("ORBY_PERMISSION_MODE", "acceptEdits"),
        "max_turns": int(os.environ.get("ORBY_MAX_TURNS", "50")),
        "allowed_tools": os.environ.get(
            "ORBY_ALLOWED_TOOLS", "Read,Write,Edit,Bash,Grep,Glob"
        ).split(","),
    }
