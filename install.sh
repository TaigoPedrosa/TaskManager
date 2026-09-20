#!/usr/bin/env bash
# TaskManager installation script for executables and AI coding harness plugins.
# Supports Claude Code, Antigravity (Gemini), and standalone terminal environments.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMAND="${1:-install}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
err()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

check_executable() {
    log "Checking TaskManager executable in PATH..."
    if command -v tm >/dev/null 2>&1; then
        local tm_path
        tm_path="$(command -v tm)"
        ok "tm is installed at: $tm_path"
        "$tm_path" --help >/dev/null 2>&1 && ok "tm executable runs successfully"
    else
        warn "tm is not found in PATH"
    fi
}

install_executable() {
    log "Installing TaskManager as global CLI executable..."
    if command -v uv >/dev/null 2>&1; then
        uv tool install --editable "$REPO_DIR" --force
        ok "Installed taskmanager via uv tool (executable in ~/.local/bin/)"
    elif command -v pipx >/dev/null 2>&1; then
        pipx install -e "$REPO_DIR" --force
        ok "Installed taskmanager via pipx"
    else
        python3 -m pip install -e "$REPO_DIR"
        ok "Installed taskmanager via pip"
    fi
}

install_claude_plugin() {
    log "Registering TaskManager plugin with Claude Code..."
    if command -v claude >/dev/null 2>&1; then
        claude plugin validate "$REPO_DIR" >/dev/null 2>&1 || true
        claude plugin marketplace add "$REPO_DIR" >/dev/null 2>&1 || true
        claude plugin install taskmanager@taskmanager >/dev/null 2>&1 || true
        ok "Registered and enabled taskmanager@taskmanager in Claude Code"
    else
        warn "claude CLI not found; linking skills to ~/.claude/skills/ directly"
        mkdir -p "$HOME/.claude/skills"
        ln -sfn "$REPO_DIR/skills/taskmanager" "$HOME/.claude/skills/taskmanager"
        ln -sfn "$REPO_DIR/skills/dispatcher" "$HOME/.claude/skills/dispatcher"
        ok "Linked skills into ~/.claude/skills/"
    fi
}

install_antigravity_plugin() {
    log "Registering TaskManager with Antigravity / Gemini CLI..."
    local gemini_plugins="$HOME/.gemini/config/plugins"
    if [ -d "$HOME/.gemini" ]; then
        mkdir -p "$gemini_plugins"
        ln -sfn "$REPO_DIR" "$gemini_plugins/taskmanager"
        ok "Linked TaskManager into $gemini_plugins/taskmanager"
    else
        warn "~/.gemini directory not present; skipping Antigravity registration"
    fi
}

uninstall_all() {
    log "Uninstalling TaskManager executable and plugins..."
    if command -v claude >/dev/null 2>&1; then
        claude plugin uninstall taskmanager >/dev/null 2>&1 || true
        claude plugin marketplace remove taskmanager >/dev/null 2>&1 || true
        ok "Uninstalled Claude Code plugin"
    fi
    rm -rf "$HOME/.claude/skills/taskmanager" "$HOME/.claude/skills/dispatcher"
    rm -rf "$HOME/.gemini/config/plugins/taskmanager"

    if command -v uv >/dev/null 2>&1; then
        uv tool uninstall taskmanager >/dev/null 2>&1 || true
        ok "Uninstalled taskmanager uv tool"
    fi
    ok "Uninstall completed"
}

show_status() {
    log "TaskManager Status Report"
    check_executable

    log "Claude Code Plugin Status:"
    if command -v claude >/dev/null 2>&1; then
        claude plugin list 2>/dev/null | grep -A 3 'taskmanager' || warn "taskmanager plugin not listed in claude"
    else
        warn "claude CLI not found"
    fi

    log "Antigravity Plugin Status:"
    if [ -e "$HOME/.gemini/config/plugins/taskmanager" ]; then
        ok "Antigravity plugin symlink active: $(readlink "$HOME/.gemini/config/plugins/taskmanager")"
    else
        warn "Antigravity plugin symlink not present"
    fi
}

case "$COMMAND" in
    install)
        install_executable
        install_claude_plugin
        install_antigravity_plugin
        echo ""
        log "Verifying installation..."
        check_executable
        ok "TaskManager is now executable and integrated into AI coding harnesses!"
        ;;
    status)
        show_status
        ;;
    uninstall)
        uninstall_all
        ;;
    *)
        echo "Usage: $0 [install|status|uninstall]"
        exit 1
        ;;
esac
