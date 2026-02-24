#!/bin/bash
set -e

# -- Colors -------------------------------------------------------------------
BOLD='\033[1m' DIM='\033[2m' RESET='\033[0m'
GREEN='\033[0;32m' CYAN='\033[0;36m' YELLOW='\033[0;33m' RED='\033[0;31m'

check() { printf "  ${GREEN}+${RESET} %s\n" "$1"; }
skip()  { printf "  ${DIM}-${RESET} ${DIM}%s${RESET}\n" "$1"; }
step()  { printf "\n${CYAN}==>${RESET} ${BOLD}%s${RESET}\n" "$1"; }
warn()  { printf "  ${YELLOW}!${RESET} %s\n" "$1"; }
fail()  { printf "  ${RED}x${RESET} %s\n" "$1"; exit 1; }

# Resolve repo directory (where this script lives)
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ORBY_DIR="$HOME/.orby"

# -- Banner -------------------------------------------------------------------
clear
cat << 'BANNER'

          ==============+===
       ===============================
      ========:......:==================
     +=====-.          .=================
     =====:.            .:===============         ___         _
     ====: :#: :+.       .===============        / _ \  _ __ | |__  _   _
     ====. :#. =*.        :==============+      | | | || '__|| '_ \| | | |
++++  ====:                -==============      | |_| || |   | |_) | |_| |
++++++ ====.             ..==============        \___/ |_|   |_.__/ \__, |
++++++++====.            .===============++++                       |___/
++++++++ ====-..      ..================ +++++
++++++++++ ========--=================== +++++++
+++++++++++ ============================++++++++
+++++++++++++ ========================== +++++++++
++++++++++++++ ======================== +++++++++++
     +++++++++  ====================== +++++++++++++
                ======================++++++++++++++
                 +=================== ++++++++++++
                  ===================  ++++
                   +===============+
                    ===============
                     =============+
                      ============
                       ==========
                        ========
                          ====
BANNER
printf "${DIM}  Slack-to-Claude Code bridge${RESET}\n"
printf "${DIM}  ────────────────────────────${RESET}\n\n"

# -- Step 0: Symlink ----------------------------------------------------------
step "Installation"

if [[ "$REPO_DIR" != "$ORBY_DIR" ]]; then
    if [[ -L "$ORBY_DIR" ]]; then
        skip "~/.orby symlink exists"
    elif [[ -d "$ORBY_DIR" ]]; then
        warn "~/.orby already exists as a directory"
        printf "  ${DIM}Backing up to ~/.orby.bak${RESET}\n"
        mv "$ORBY_DIR" "$HOME/.orby.bak"
        ln -s "$REPO_DIR" "$ORBY_DIR"
        check "Symlinked $REPO_DIR -> ~/.orby"
    else
        ln -s "$REPO_DIR" "$ORBY_DIR"
        check "Symlinked $REPO_DIR -> ~/.orby"
    fi
else
    skip "Already running from ~/.orby"
fi

# -- Step 1: Python -----------------------------------------------------------
step "Python dependencies"

if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.10+ first."
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
check "Python $PY_VER"

if [[ ! -d "$ORBY_DIR/.venv" ]]; then
    python3 -m venv "$ORBY_DIR/.venv"
    check "Created virtualenv"
else
    skip "Virtualenv exists"
fi

"$ORBY_DIR/.venv/bin/pip" install -q --upgrade pip
"$ORBY_DIR/.venv/bin/pip" install -q -r "$ORBY_DIR/requirements.txt"
check "Installed slack-bolt, claude-agent-sdk"

# -- Step 2: Slack App --------------------------------------------------------
step "Slack App configuration"

SKIP_SLACK=""
if [[ -f "$ORBY_DIR/config.env" ]] && grep -q "SLACK_BOT_TOKEN" "$ORBY_DIR/config.env"; then
    skip "config.env already exists"
    printf "  "
    read -p "Reconfigure? [y/N] " reconf
    [[ "$reconf" =~ ^[Yy]$ ]] || SKIP_SLACK=true
fi

if [[ -z "$SKIP_SLACK" ]]; then
    printf "\n"
    printf "  ${BOLD}Create a Slack App (one-time setup):${RESET}\n\n"
    printf "  ${DIM}1.${RESET} Go to ${CYAN}https://api.slack.com/apps${RESET}\n"
    printf "  ${DIM}2.${RESET} Create New App > From scratch > name it ${BOLD}Orby${RESET}\n"
    printf "  ${DIM}3.${RESET} Settings > ${BOLD}Socket Mode${RESET} > Enable > name token 'orby' > Generate\n"
    printf "\n"
    read -p "  SLACK_APP_TOKEN (xapp-...): " APP_TOKEN
    echo

    printf "  ${DIM}4.${RESET} OAuth & Permissions > add ${BOLD}Bot Token Scopes${RESET}:\n"
    printf "     ${DIM}app_mentions:read  chat:write  im:history  im:read  im:write${RESET}\n"
    printf "  ${DIM}5.${RESET} Install App to Workspace > copy Bot User OAuth Token\n"
    printf "\n"
    read -p "  SLACK_BOT_TOKEN (xoxb-...): " BOT_TOKEN
    echo

    printf "  ${DIM}6.${RESET} Event Subscriptions > Enable > Subscribe to bot events:\n"
    printf "     ${DIM}app_mention  message.im${RESET}\n"
    printf "  ${DIM}7.${RESET} App Home > Messages Tab > check 'Allow users to send...'\n"
    printf "\n"
    read -p "  Press Enter when done... "
    echo

    cat > "$ORBY_DIR/config.env" << EOF
SLACK_BOT_TOKEN=$BOT_TOKEN
SLACK_APP_TOKEN=$APP_TOKEN
EOF
    check "Saved tokens to config.env"
fi

# -- Step 3: Preferences ------------------------------------------------------
step "Preferences"

printf "\n"
read -p "  Default working directory [$HOME]: " DEFAULT_CWD
DEFAULT_CWD="${DEFAULT_CWD:-$HOME}"

read -p "  Permission mode (acceptEdits/default/bypassPermissions) [acceptEdits]: " PERM_MODE
PERM_MODE="${PERM_MODE:-acceptEdits}"

sed -i '/^ORBY_DEFAULT_CWD=/d; /^ORBY_PERMISSION_MODE=/d' "$ORBY_DIR/config.env" 2>/dev/null || true
cat >> "$ORBY_DIR/config.env" << EOF
ORBY_DEFAULT_CWD=$DEFAULT_CWD
ORBY_PERMISSION_MODE=$PERM_MODE
EOF
check "Preferences saved"

# -- Step 4: Claude Code hooks ------------------------------------------------
step "Claude Code hooks"

CLAUDE_SETTINGS="$HOME/.claude/settings.json"
HOOK_CMD="$ORBY_DIR/.venv/bin/python3 $ORBY_DIR/hooks/notify.py"

if [[ -f "$CLAUDE_SETTINGS" ]] && grep -q "notify.py" "$CLAUDE_SETTINGS"; then
    skip "Hooks already configured in settings.json"
else
    printf "  ${DIM}Add Orby hooks to ~/.claude/settings.json for attach mode?${RESET}\n"
    printf "  ${DIM}(hooks forward Claude activity to Slack when using !attach)${RESET}\n\n"
    read -p "  Add hooks? [Y/n] " ADD_HOOKS
    if [[ ! "$ADD_HOOKS" =~ ^[Nn]$ ]]; then
        if command -v python3 &>/dev/null; then
            python3 - "$CLAUDE_SETTINGS" "$HOOK_CMD" << 'PYEOF'
import json, sys
settings_path, hook_cmd = sys.argv[1], sys.argv[2]
try:
    with open(settings_path) as f:
        settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}
hooks = settings.setdefault("hooks", {})
for event in ["Stop", "PreToolUse", "Notification"]:
    event_hooks = hooks.setdefault(event, [])
    hook_entry = {"type": "command", "command": f"{hook_cmd} --event {event}", "async": True}
    # Check if already present
    existing_cmds = [h.get("command","") for group in event_hooks for h in group.get("hooks",[])]
    if any("notify.py" in c for c in existing_cmds):
        continue
    if event_hooks and "hooks" in event_hooks[0]:
        event_hooks[0]["hooks"].append(hook_entry)
    else:
        event_hooks.append({"matcher": "", "hooks": [hook_entry]})
with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
print("  + Hooks added to settings.json")
PYEOF
        fi
    fi
fi

# -- Step 5: Shell aliases ----------------------------------------------------
step "Shell aliases"

ALIAS_BLOCK='
# ── Orby (Slack-to-Claude Code bridge) ──────────────────────────────────────
alias orby='"'"'tmux attach-session -t orby 2>/dev/null || echo "Orby is not running. Run: orby-start"'"'"'
orby-start() {
    if tmux has-session -t orby 2>/dev/null; then echo "Orby is already running"; return 0; fi
    tmux new-session -d -s orby "$HOME/.orby/.venv/bin/python3 $HOME/.orby/bot.py"
    echo "Orby started (tmux session: orby)"
}
orby-stop() { tmux kill-session -t orby 2>/dev/null && echo "Orby stopped" || echo "Orby is not running"; }
orby-status() { tmux has-session -t orby 2>/dev/null && echo "Orby is running" || echo "Orby is not running"; }
alias orby-logs='"'"'tail -f ~/.orby/orby.log'"'"'
alias orby-install='"'"'bash ~/.orby/install.sh'"'"'
'

# Detect shell config file
if [[ -f "$HOME/.aliases.zsh" ]]; then
    SHELL_RC="$HOME/.aliases.zsh"
elif [[ -f "$HOME/.zshrc" ]]; then
    SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [[ -n "$SHELL_RC" ]] && grep -q "orby-start" "$SHELL_RC"; then
    skip "Aliases already in $SHELL_RC"
else
    printf "  Add orby shell aliases to ${BOLD}$SHELL_RC${RESET}?\n\n"
    read -p "  Add aliases? [Y/n] " ADD_ALIASES
    if [[ ! "$ADD_ALIASES" =~ ^[Nn]$ ]]; then
        echo "$ALIAS_BLOCK" >> "$SHELL_RC"
        check "Added aliases to $SHELL_RC"
    fi
fi

# -- Step 6: Verify -----------------------------------------------------------
step "Verifying installation"

"$ORBY_DIR/.venv/bin/python3" -c "from slack_bolt.app.async_app import AsyncApp" && check "slack-bolt OK" || fail "slack-bolt import failed"
"$ORBY_DIR/.venv/bin/python3" -c "from claude_agent_sdk import query" && check "claude-agent-sdk OK" || fail "claude-agent-sdk import failed"

# -- Step 7: Launch -----------------------------------------------------------
step "Launch"

printf "\n"
read -p "  Start Orby now? [Y/n] " START_NOW
if [[ ! "$START_NOW" =~ ^[Nn]$ ]]; then
    if tmux has-session -t orby 2>/dev/null; then
        warn "tmux session 'orby' already exists"
    else
        tmux new-session -d -s orby "$ORBY_DIR/.venv/bin/python3 $ORBY_DIR/bot.py"
        check "Orby running in tmux session 'orby'"
    fi
fi

# -- Done ---------------------------------------------------------------------
printf "\n"
printf "  ${GREEN}────────────────────────────────────────${RESET}\n"
printf "  ${GREEN}${BOLD}Orby is ready.${RESET}\n"
printf "  ${GREEN}────────────────────────────────────────${RESET}\n\n"
printf "  ${BOLD}Commands:${RESET}\n"
printf "    ${GREEN}orby${RESET}             Attach to tmux session\n"
printf "    ${GREEN}orby-start${RESET}       Start the bot\n"
printf "    ${GREEN}orby-stop${RESET}        Stop the bot\n"
printf "    ${GREEN}orby-status${RESET}      Check if running\n"
printf "    ${GREEN}orby-logs${RESET}        Tail the log file\n"
printf "\n"
printf "  ${BOLD}Slack commands:${RESET}\n"
printf "    ${CYAN}!attach <id>${RESET}     Attach to a Claude session by ID or tmux name\n"
printf "    ${CYAN}!detach${RESET}          Detach from attached session\n"
printf "    ${CYAN}!screen${RESET}          Show tmux pane content\n"
printf "    ${CYAN}!status${RESET}          Show session info\n"
printf "    ${CYAN}!sessions${RESET}        List available tmux sessions\n"
printf "\n"
