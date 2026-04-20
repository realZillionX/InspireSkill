#!/usr/bin/env bash
# InspireSkill developer installer — editable CLI + symlinked SKILL.
#
# Use from a local checkout of the repo. Intended for maintainers iterating
# on SKILL.md / CLI code; end users should use scripts/install.sh (which pulls
# from GitHub and does not require a local clone).
#
# What it does (in-place, via symlinks back into $SKILL_ROOT):
#   - `uv sync` at cli/  → editable venv at cli/.venv
#   - symlinks cli/.venv/bin/inspire → ~/.local/bin/inspire (so the ~/.zshrc
#     proxy wrapper `inspire()` can find it)
#   - symlinks SKILL.md + references/ → ~/.{claude,codex,gemini}/skills/inspire/
#   - emits Codex's agents/openai.yaml
#   - installs the same macOS launchd update-check agent as install.sh
#   - strips UF_HIDDEN from .pth files (macOS iCloud/Desktop workaround;
#     otherwise site.py silently skips the editable finder)
#
# Flags mirror install.sh: --harness / --no-cli / --no-schedule.
#
# Exit codes: 0 ok · 1 precondition · 2 sync · 3 link.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLI_DIR="$SKILL_ROOT/cli"
LAUNCH_LABEL="sh.inspire-skill.update-check"

HARNESSES=""
INSTALL_CLI=1
INSTALL_SCHEDULE=1

color()  { local c="$1"; shift; printf '\033[%sm%s\033[0m' "$c" "$*"; }
bold()   { color "1"  "$@"; }
dim()    { color "2"  "$@"; }
red()    { color "31" "$@"; }
green()  { color "32" "$@"; }
yellow() { color "33" "$@"; }
blue()   { color "34" "$@"; }
log()    { printf '%s %s\n' "$(blue '›')" "$*"; }
ok()     { printf '%s %s\n' "$(green '✓')" "$*"; }
warn()   { printf '%s %s\n' "$(yellow '!')" "$*"; }
die()    { printf '%s %s\n' "$(red '✗')" "$*" >&2; exit "${2:-1}"; }

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness)      HARNESSES="$2";       shift 2 ;;
    --harness=*)    HARNESSES="${1#*=}";  shift ;;
    --no-cli)       INSTALL_CLI=0;        shift ;;
    --no-schedule)  INSTALL_SCHEDULE=0;   shift ;;
    -h|--help)      usage ;;
    *)              die "unknown argument: $1" ;;
  esac
done

# ---- harness detection -----------------------------------------------------
detect_harnesses() {
  local found=()
  [[ -d "$HOME/.claude"                                      ]] && found+=("claude")
  [[ -d "$HOME/.codex"                                       ]] && found+=("codex")
  [[ -d "$HOME/.gemini"                                      ]] && found+=("gemini")
  [[ -d "$HOME/.openclaw"                                    ]] && found+=("openclaw")
  [[ -d "${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"     ]] && found+=("opencode")
  (IFS=,; echo "${found[*]:-}")
}

if [[ -z "$HARNESSES" ]]; then
  HARNESSES="$(detect_harnesses)"
  [[ -n "$HARNESSES" ]] || die "no agent harness under \$HOME. Pass --harness."
  log "auto-detected harnesses: $(bold "$HARNESSES")"
fi

IFS=',' read -r -a HARNESS_LIST <<<"$HARNESSES"
for h in "${HARNESS_LIST[@]}"; do
  case "$h" in claude|codex|gemini|openclaw|opencode) ;; *) die "unknown harness: $h";; esac
done

[[ -d "$CLI_DIR/inspire" && -f "$CLI_DIR/pyproject.toml" ]] \
  || die "vendored CLI not found under $CLI_DIR — re-clone InspireSkill." 1

# ---- editable CLI install via uv ------------------------------------------
if (( INSTALL_CLI )); then
  command -v uv >/dev/null 2>&1 || die "install-dev requires uv (curl -LsSf https://astral.sh/uv/install.sh | sh)."
  log "syncing $(dim "$CLI_DIR") via uv"
  (cd "$CLI_DIR" && uv sync --quiet) || die "uv sync failed" 2

  # macOS Desktop/iCloud often applies UF_HIDDEN to freshly written files;
  # site.py then skips .pth files and `import inspire` fails. Scrub the flag.
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if chflags -R nohidden "$CLI_DIR/.venv/lib" 2>/dev/null; then
      dim "(chflags nohidden applied to venv)" ; echo
    fi
  fi

  # Make the venv's inspire visible to the user's ~/.zshrc wrapper.
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$CLI_DIR/.venv/bin/inspire" "$HOME/.local/bin/inspire"
  ok "linked $(bold 'inspire') → $(dim "$CLI_DIR/.venv/bin/inspire")"

  # Clean up stale inspire-update shim from earlier script versions.
  [[ -L "$HOME/.local/bin/inspire-update" ]] && rm -f "$HOME/.local/bin/inspire-update"
fi

# ---- link SKILL into each harness -----------------------------------------
link_skill() {
  local harness="$1"
  local target
  case "$harness" in
    claude)   target="$HOME/.claude/skills/inspire"                                    ;;
    codex)    target="$HOME/.codex/skills/inspire"                                     ;;
    gemini)   target="$HOME/.gemini/skills/inspire"                                    ;;
    openclaw) target="$HOME/.openclaw/skills/inspire"                                  ;;
    opencode) target="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}/skills/inspire"   ;;
  esac

  # Replace any prior user-mode install (real files) with dev-mode symlinks.
  if [[ -e "$target" || -L "$target" ]]; then
    rm -rf "$target"
  fi
  mkdir -p "$target"

  ln -sfn "$SKILL_ROOT/SKILL.md" "$target/SKILL.md"
  [[ -d "$SKILL_ROOT/references" ]] && ln -sfn "$SKILL_ROOT/references" "$target/references"

  if [[ "$harness" == "codex" ]]; then
    mkdir -p "$target/agents"
    cat >"$target/agents/openai.yaml" <<'YAML'
interface:
  display_name: "Inspire"
  short_description: "Execution-first Inspire operations via the inspire CLI, including auth, proxy routing, notebook/image workflows, and job/HPC execution."
YAML
  fi

  ok "linked skill → $(dim "$target")"
}

for h in "${HARNESS_LIST[@]}"; do
  link_skill "$h"
done

# ---- launchd update-check agent (same plist as install.sh) -----------------
install_launch_agent() {
  local inspire_path="$HOME/.local/bin/inspire"
  [[ -x "$inspire_path" ]] || inspire_path="$(command -v inspire || true)"
  [[ -n "$inspire_path" ]] || { warn "no inspire on PATH; skipping launchd agent."; return 0; }

  local plist="$HOME/Library/LaunchAgents/${LAUNCH_LABEL}.plist"
  local log_file="$HOME/Library/Logs/inspire-skill-update-check.log"
  mkdir -p "$(dirname "$plist")" "$(dirname "$log_file")"

  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>                 <string>${LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${inspire_path}</string>
    <string>update</string>
    <string>--check</string>
    <string>--silent</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>INSPIRE_SKIP_UPDATE_CHECK</key> <string>1</string>
    <key>http_proxy</key>                <string>http://127.0.0.1:7897</string>
    <key>https_proxy</key>               <string>http://127.0.0.1:7897</string>
    <key>HTTP_PROXY</key>                <string>http://127.0.0.1:7897</string>
    <key>HTTPS_PROXY</key>               <string>http://127.0.0.1:7897</string>
  </dict>
  <key>StartInterval</key>         <integer>86400</integer>
  <key>RunAtLoad</key>             <true/>
  <key>StandardOutPath</key>       <string>${log_file}</string>
  <key>StandardErrorPath</key>     <string>${log_file}</string>
</dict>
</plist>
PLIST

  launchctl unload "$plist" >/dev/null 2>&1 || true
  if launchctl load "$plist" 2>/dev/null; then
    ok "update-check agent loaded $(dim "$plist")"
  else
    warn "couldn't load launchd agent (plist at $plist)."
  fi
}

if (( INSTALL_SCHEDULE )) && [[ "$(uname -s)" == "Darwin" ]]; then
  install_launch_agent
fi

# ---- smoke test ------------------------------------------------------------
if command -v inspire >/dev/null 2>&1; then
  if ! INSPIRE_SKIP_UPDATE_CHECK=1 inspire --version >/dev/null 2>&1; then
    die "inspire is on PATH but crashes on --version; check cli/.venv." 2
  fi
  ok "$(INSPIRE_SKIP_UPDATE_CHECK=1 inspire --version)"
fi

echo
bold "InspireSkill (dev) installed from $SKILL_ROOT."
cat <<'EOF'
  Your local edits to SKILL.md / references/*/cli/ source take effect immediately
  via the symlinks + editable venv. No `inspire update` needed during dev.

  To publish a change:  git push — end users run `inspire update` to pick it up.
EOF
