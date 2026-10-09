#!/usr/bin/env bash
# Installs, reports on and uninstalls tm and its Claude Code plugin at one version.
#   ./install.sh [install|status|uninstall] [--ref <git ref>] [--from <checkout>]
#   curl -fsSL https://raw.githubusercontent.com/TaigoPedrosa/TaskManager/main/install.sh | bash -s -- [args]
set -euo pipefail

REPO=TaigoPedrosa/TaskManager
PLUGIN=taskmanager@taskmanager
MARKETPLACE=taskmanager
CODEGRAPH_FIX="npm install -g @colbymchenry/codegraph"

usage() {
    cat <<EOF
Usage: install.sh [install|status|uninstall] [--ref <git ref>] [--from <checkout>]

  install     install tm (uv tool) and the Claude Code plugin at one version (default)
  status      report tm, the plugin, the Gemini link and codegraph; non-zero when tm or
              the plugin is missing or their versions differ
  uninstall   remove what install created

  --ref <ref>       git ref of $REPO to install (default: main)
  --from <path>     install from a local checkout instead of GitHub; --ref is ignored

The plugin goes into the Claude Code profile CLAUDE_CONFIG_DIR names (~/.claude when unset).
EOF
}

say() { printf '%s\n' "$*"; }
fail() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

COMMAND=install
REF=main
FROM=
while [ $# -gt 0 ]; do
    case "$1" in
        install | status | uninstall) COMMAND=$1 ;;
        --ref)
            [ $# -ge 2 ] || fail "--ref needs a git ref"
            REF=$2
            shift
            ;;
        --from)
            [ $# -ge 2 ] || fail "--from needs a checkout path"
            FROM=$2
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [ -n "$FROM" ]; then
    FROM=$(cd "$FROM" 2>/dev/null && pwd) || fail "--from: no such directory"
    [ -f "$FROM/pyproject.toml" ] || fail "--from $FROM: not a TaskManager checkout (no pyproject.toml)"
    TOOL_SOURCE=$FROM
    MARKETPLACE_SOURCE=$FROM
else
    TOOL_SOURCE="git+https://github.com/$REPO@$REF"
    MARKETPLACE_SOURCE="https://github.com/$REPO.git#$REF"
fi

PROFILE=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
GEMINI_LINK=$HOME/.gemini/extensions/taskmanager

has() { command -v "$1" >/dev/null 2>&1; }

tm_bin() {
    if has uv; then
        printf '%s/tm\n' "$(uv tool dir --bin)"
    else
        command -v tm || true
    fi
}

tm_version() {
    local bin
    bin=$(tm_bin)
    [ -n "$bin" ] && [ -x "$bin" ] || return 0
    "$bin" --version 2>/dev/null | awk '{print $NF}' || true
}

# Reads the version off `claude plugin list --json`, pretty-printed or compact.
plugin_version() {
    has claude || return 0
    claude plugin list --json 2>/dev/null | tr -d '\n' | tr '{}' '\n' \
        | grep "\"$PLUGIN\"" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' | head -n 1 || true
}

marketplace_added() {
    claude plugin marketplace list --json 2>/dev/null | tr -d ' \n' \
        | grep -q "\"name\":\"$MARKETPLACE\""
}

json_field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p"; }

# The source the marketplace was added from, in the form `marketplace add` takes.
marketplace_source() {
    local entry path url ref
    entry=$(claude plugin marketplace list --json 2>/dev/null | tr -d '\n' | tr '{}' '\n' \
        | grep "\"name\": *\"$MARKETPLACE\"" | head -n 1) || return 0
    path=$(printf "%s" "$entry" | json_field path)
    url=$(printf "%s" "$entry" | json_field url)
    ref=$(printf "%s" "$entry" | json_field ref)
    if [ -n "$path" ]; then
        printf '%s\n' "$path"
    else
        printf '%s\n' "$url${ref:+#$ref}"
    fi
}

gemini_source() {
    if [ -n "$FROM" ]; then
        printf '%s\n' "$FROM"
    elif [ -f "$PROFILE/plugins/marketplaces/$MARKETPLACE/gemini-extension.json" ]; then
        printf '%s\n' "$PROFILE/plugins/marketplaces/$MARKETPLACE"
    fi
}

report_codegraph() {
    if has codegraph; then
        say "codegraph: $(command -v codegraph)"
    else
        say "codegraph: absent (optional): $CODEGRAPH_FIX"
    fi
}

install() {
    has uv || fail "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
    has git || fail "git is required: https://git-scm.com/downloads"

    say "tm: uv tool install $TOOL_SOURCE"
    uv tool install --force --reinstall "$TOOL_SOURCE"

    if has claude; then
        say "plugin: $PLUGIN from $MARKETPLACE_SOURCE into $PROFILE"
        # Claude Code refuses to add a git source over a marketplace added from another source.
        local added
        added=$(marketplace_source)
        if [ -n "$added" ] && [ "$added" != "$MARKETPLACE_SOURCE" ]; then
            claude plugin marketplace remove "$MARKETPLACE"
        fi
        claude plugin marketplace add "$MARKETPLACE_SOURCE"
        claude plugin marketplace update "$MARKETPLACE"
        claude plugin install "$PLUGIN"
        claude plugin update "$PLUGIN"
    else
        say "plugin: claude is not on PATH; once it is, run:"
        say "  claude plugin marketplace add $MARKETPLACE_SOURCE"
        say "  claude plugin install $PLUGIN"
    fi

    local source
    source=$(gemini_source)
    if [ ! -d "$HOME/.gemini" ]; then
        say "gemini: skipped, ~/.gemini does not exist"
    elif [ ! -d "$(dirname "$GEMINI_LINK")" ]; then
        say "gemini: skipped, ~/.gemini/extensions does not exist"
    elif [ -e "$GEMINI_LINK" ] && [ ! -L "$GEMINI_LINK" ]; then
        say "gemini: skipped, $GEMINI_LINK exists and is not a link"
    elif [ -z "$source" ]; then
        say "gemini: skipped, no local copy of the extension (install the plugin or use --from)"
    else
        ln -sfn "$source" "$GEMINI_LINK"
        say "gemini: $GEMINI_LINK -> $source"
    fi

    local tm plugin
    tm=$(tm_version)
    [ -n "$tm" ] || fail "tm --version did not run from $(tm_bin)"
    plugin=$(plugin_version)
    if has claude; then
        [ -n "$plugin" ] || fail "$PLUGIN is not installed in $PROFILE"
        [ "$tm" = "$plugin" ] || fail "tm $tm differs from plugin $plugin"
    fi
    say "tm $tm installed at $(tm_bin)${plugin:+, plugin $plugin}"
    report_codegraph
}

status() {
    local rc=0 tm plugin
    tm=$(tm_version)
    if [ -n "$tm" ]; then
        say "tm: $(tm_bin) $tm"
    else
        say "tm: not installed"
        rc=1
    fi

    if ! has claude; then
        say "plugin: claude is not on PATH"
        rc=1
    else
        plugin=$(plugin_version)
        if [ -n "$plugin" ]; then
            say "plugin: $PLUGIN $plugin in $PROFILE"
        else
            say "plugin: $PLUGIN not installed in $PROFILE"
            rc=1
        fi
        if [ -n "$tm" ] && [ -n "$plugin" ] && [ "$tm" != "$plugin" ]; then
            say "mismatch: tm $tm differs from plugin $plugin"
            rc=1
        fi
    fi

    if [ -L "$GEMINI_LINK" ]; then
        say "gemini: $GEMINI_LINK -> $(readlink "$GEMINI_LINK")"
    else
        say "gemini: not linked"
    fi
    report_codegraph
    return "$rc"
}

uninstall() {
    if has uv && uv tool list 2>/dev/null | grep -q '^taskmanager '; then
        uv tool uninstall taskmanager
    fi
    if has claude; then
        if [ -n "$(plugin_version)" ]; then
            claude plugin uninstall "$PLUGIN"
        fi
        if marketplace_added; then
            claude plugin marketplace remove "$MARKETPLACE"
        fi
    fi
    if [ -L "$GEMINI_LINK" ]; then
        rm "$GEMINI_LINK"
    fi
    say "uninstalled"
}

"$COMMAND"
