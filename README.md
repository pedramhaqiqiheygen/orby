```
               =================
            =======--===============
           ====-        :============
          ====:          .============
          +===.-* +-      -===========        ___         _
       +++ ===.           -==========        / _ \  _ __ | |__  _   _
       ++++ ==-.         .========== ++     | | | || '__|| '_ \| | | |
      ++++++ ===.      .============++++    | |_| || |   | |_) | |_| |
      +++++++ ===================== +++++    \___/ |_|   |_.__/ \__, |
     +++++++++ =================== ++++++                       |___/
     ++++++++++ =================+++++++++
          +++++ +================ +++++++++
                 =============== ++++++++++
                  =============  ++
                   ===========+
                    +=========
                     ========
                      =======
                        +=+
```

# Orby

An extensible Slack-to-agent bridge. Control AI coding agents from Slack — DM them, attach to live sessions, approve/reject actions, and get real-time updates in threads.

## Why

You're running Claude Code in tmux on a remote machine. You step away from your desk. You want to check on it, send a follow-up, or approve a permission prompt — from your phone, from Slack, from anywhere. Orby bridges that gap.

## How It Works

Orby runs as a Slack bot (Socket Mode — no public URLs, purely outbound, TLS encrypted). Messages in Slack threads route to agent backends that do the actual work. Responses stream back to the thread.

```
Slack DM / @mention
        │
        ▼
   ┌─────────┐
   │  Router  │──── !attach, !detach, !screen, !status, approve, reject
   │ (bot.py) │
   └────┬─────┘
        │
   ┌────┴────┐
   │         │
   ▼         ▼
SDK Agent   Tmux Agent
   │         │
   │         ├── tmux send-keys (inject messages)
   │         └── Claude Code hooks (receive responses)
   │
   └── claude-agent-sdk query() with session resume
```

### Two Modes

**SDK Mode** (default) — Each Slack thread gets its own Claude Code session. Messages go through the Agent SDK programmatically. Responses stream back with progressive updates. Sessions persist across bot restarts.

**Attach Mode** — Bridge a live Claude Code CLI session running in tmux. Messages are injected via `tmux send-keys`. Claude Code's hook system forwards responses, tool use, and notifications back to the Slack thread. You can approve/reject permission prompts and interrupt tasks remotely.

## Supported Agents

| Agent | Mode | Description |
|-------|------|-------------|
| **Claude Code (SDK)** | SDK Mode | Programmatic sessions via `claude-agent-sdk`. Auto-creates and resumes sessions per thread. |
| **Claude Code (tmux)** | Attach Mode | Bridges to a running interactive CLI session. Bidirectional via `send-keys` + hooks. |

The agent abstraction (`core/agent.py`) is designed for extension. New agents implement `send()`, `interrupt()`, `approve()`, `reject()`.

## Slack Commands

| Command | Description |
|---------|-------------|
| `!attach <session-id>` | Attach thread to a Claude Code session by UUID. Launches it in tmux with `--resume`. |
| `!attach <tmux-name>` | Attach thread to an already-running tmux session. |
| `!detach` | Detach from tmux session, revert to SDK mode. |
| `!screen` | Capture and post the current tmux pane content. |
| `!status` | Show session info (agent type, session ID, working dir). |
| `!sessions` | List available tmux sessions. |
| `approve` / `y` | Approve a pending permission prompt (attach mode). |
| `reject` / `n` | Reject a pending permission prompt (attach mode). |
| `interrupt` / `stop` | Send Ctrl+C to interrupt Claude (attach mode). |
| `//skill-name [args]` | Invoke a Claude Code skill (e.g., `//commit`, `//investigate`). |
| `!skills` | List available Claude Code skills. |

## Install

```bash
git clone https://github.com/pedramhaqiqiheygen/orby.git
cd orby
bash install.sh
```

The installer will:
1. Symlink the repo to `~/.orby`
2. Create a Python virtualenv and install dependencies
3. Walk you through creating a Slack App (Socket Mode)
4. Configure Claude Code hooks for attach mode
5. Add shell aliases (`orby-start`, `orby-stop`, `orby-status`, `orby-logs`)

### Prerequisites

- Python 3.10+
- tmux
- Claude Code CLI installed and authenticated
- A Slack workspace where you can create apps

### Slack App Setup

The installer guides you through this, but in short:

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** (generates `xapp-` token)
3. Add bot scopes: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
4. Subscribe to events: `app_mention`, `message.im`
5. Enable App Home > Messages Tab
6. Install to workspace (generates `xoxb-` token)

## Architecture

```
~/.orby/
  bot.py              # Slack event handler + command router
  config.py           # Config loader (~/.orby/config.env)
  formatter.py        # Slack message formatting + splitting
  core/
    agent.py          # Abstract Agent base class
    session.py        # Thread-to-session persistence (JSON)
  agents/
    sdk_agent.py      # Claude Agent SDK backend
    tmux_agent.py     # tmux send-keys/capture-pane backend
  hooks/
    notify.py         # Claude Code hook → Slack thread forwarder
  install.sh          # Interactive TUI installer
  config.env          # Secrets (not tracked)
  sessions.json       # Session state (not tracked)
```

### Security

- **Socket Mode**: Purely outbound WSS connection. No open ports, no public URLs.
- **Minimal scopes**: Bot can only read messages where it's mentioned or DM'd, and post replies.
- **Tokens stored locally**: `config.env` is gitignored, never leaves your machine.
- **No data exfiltration**: All processing happens locally. Slack only sees the text responses.

## Shell Commands

After installation:

```bash
orby-start    # Start the bot in a tmux session
orby-stop     # Stop the bot
orby-status   # Check if running
orby-logs     # Tail the log file
orby          # Attach to the bot's tmux session
```

## License

MIT
