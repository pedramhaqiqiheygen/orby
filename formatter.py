"""Slack message formatting utilities.

Converts Claude tool use events into human-readable one-liners
and handles message splitting for Slack's character limits.
"""

SLACK_MSG_LIMIT = 3800  # Practical limit (hard limit is 40k, but >4k gets unreadable)


def format_tool_use(tool_name: str, tool_input: dict) -> str:
    """Return a one-line summary of a Claude tool invocation."""
    match tool_name:
        case "Bash":
            cmd = tool_input.get("command", "")
            truncated = f"{cmd[:80]}..." if len(cmd) > 80 else cmd
            return f"`$ {truncated}`"
        case "Read":
            return f"Reading `{tool_input.get('file_path', '?')}`"
        case "Write" | "Edit":
            return f"Editing `{tool_input.get('file_path', '?')}`"
        case "Grep" | "Glob":
            return f"Searching: `{tool_input.get('pattern', '?')}`"
        case _:
            return f"Using {tool_name}"


def split_message(text: str, limit: int = SLACK_MSG_LIMIT) -> list[str]:
    """Split text into chunks at natural boundaries for Slack."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Prefer splitting at double-newline, then single-newline, then hard cut
        split_at = text.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
