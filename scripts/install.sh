#!/usr/bin/env bash
# InspireSkill installer — published package plus managed skill files.
#
# Reads: none (self-contained tarball + uv/pipx download)
# Writes:
#   - ~/.local/bin/inspire       (uv tool / pipx shim; installer-managed)
#   - supported harness skill dirs, e.g. ~/.claude/skills/inspire/
#   - ~/Library/LaunchAgents/sh.inspire-skill.update-check.plist  (macOS only)
#   - ~/.inspire/update-status.json  (via post-install `inspire update --check`)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --harness claude,codex
#   curl -fsSL .../install.sh | bash -s -- --no-schedule
#
# Flags:
#   --harness claude[,codex,antigravity,cursor,openclaw,opencode,qoder,kimi-code]
#                                     explicit harness list (default: auto-detect)
#   --no-cli                          skip installing the Python package (skill-only)
#   --no-schedule                     skip the macOS launchd update-check agent
#
set -euo pipefail

REPO_SLUG="realZillionX/InspireSkill"
PACKAGE="inspire-skill"
DEFAULT_REF="main"
LAUNCH_LABEL="sh.inspire-skill.update-check"

HARNESSES=""
INSTALL_CLI=1
INSTALL_SCHEDULE=1
INSTALLER=""

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
die()    { printf '%s %s\n' "$(red '✗')" "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness)       HARNESSES="$2";       shift 2 ;;
    --harness=*)     HARNESSES="${1#*=}";  shift ;;
    --no-cli)        INSTALL_CLI=0;        shift ;;
    --no-schedule)   INSTALL_SCHEDULE=0;   shift ;;
    -h|--help)       usage ;;
    *)               die "unknown argument: $1" ;;
  esac
done

# ---- harness detection -----------------------------------------------------
detect_harnesses() {
  local found=()
  [[ -d "$HOME/.claude"                                      ]] && found+=("claude")
  [[ -d "$HOME/.codex"                                       ]] && found+=("codex")
  [[ -d "$HOME/.gemini"                                      ]] && found+=("antigravity")
  [[ -d "$HOME/.cursor"                                      ]] && found+=("cursor")
  [[ -d "$HOME/.openclaw"                                    ]] && found+=("openclaw")
  [[ -d "${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"     ]] && found+=("opencode")
  [[ -d "$HOME/.qoder"                                       ]] && found+=("qoder")
  [[ -d "$HOME/.kimi-code"                                   ]] && found+=("kimi-code")
  (IFS=,; echo "${found[*]:-}")
}

if [[ -z "$HARNESSES" ]]; then
  HARNESSES="$(detect_harnesses)"
  [[ -n "$HARNESSES" ]] \
    || die "no agent harness detected (checked \$HOME/.claude, .codex, .gemini, .cursor, .openclaw, \$OPENCODE_CONFIG_DIR or \$HOME/.config/opencode, .qoder, and .kimi-code). Pass --harness explicitly."
  log "auto-detected harnesses: $(bold "$HARNESSES")"
fi

IFS=',' read -r -a HARNESS_LIST <<<"$HARNESSES"
for h in "${HARNESS_LIST[@]}"; do
  case "$h" in
    claude|codex|antigravity|cursor|openclaw|opencode|qoder|kimi-code) ;;
    *) die "unknown harness: $h (pick from claude,codex,antigravity,cursor,openclaw,opencode,qoder,kimi-code)" ;;
  esac
done

# ---- prerequisites ---------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "need '$1' on PATH."; }
need curl
need tar
need mktemp

installed_inspire_python() {
  local shebang py
  IFS= read -r shebang < "$INSPIRE_BIN" || return 1
  [[ "$shebang" == "#!"* ]] || return 1
  py="${shebang#\#!}"
  [[ -x "$py" ]] || return 1
  printf '%s\n' "$py"
}

playwright_install_args() {
  if [[ "$(uname -s)" == "Linux" ]] && [[ "$(id -u)" == "0" ]] && command -v apt-get >/dev/null 2>&1; then
    printf '%s\n' install --with-deps chromium
  else
    printf '%s\n' install chromium
  fi
}

legacy_playwright_runtime_setup() {
  local py
  py="$(installed_inspire_python)" || return 1
  "$py" -m playwright $(playwright_install_args) || return 1
  "$py" - <<'PY'
from inspire.platform.web.session.browser_launch import chromium_launch_kwargs
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(**chromium_launch_kwargs(headless=True))
    browser.close()
PY
}

# ---- install CLI via uv tool / pipx ----------------------------------------
# Install from PyPI, so the user path stays on published releases.
SPEC="$PACKAGE"
SPEC_LABEL="$(bold "$PACKAGE") (PyPI)"

if (( INSTALL_CLI )); then
  if command -v uv >/dev/null 2>&1; then
    INSTALLER="uv"
    log "installing $SPEC_LABEL via $(bold 'uv tool')"
    uv tool install --force --refresh "$SPEC" || die "uv tool install failed — check the spec '$SPEC' and try again."
    # If a previous run installed the same package via pipx, leaving it around
    # would create two `inspire` shims competing for ~/.local/bin/inspire.
    if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q "^${PACKAGE} "; then
      log "removing earlier pipx install of $(bold "$PACKAGE") (uv tool now owns it)"
      pipx uninstall "$PACKAGE" >/dev/null 2>&1 || true
    fi
  elif command -v pipx >/dev/null 2>&1; then
    INSTALLER="pipx"
    log "installing $SPEC_LABEL via $(bold pipx)"
    pipx install --force "$SPEC" || die "pipx install failed — check the spec '$SPEC' and try again."
  else
    die "need uv or pipx. Install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi

  # Clean up stale shims from earlier installer paths.
  [[ -L "$HOME/.local/bin/inspire-update" ]] && rm -f "$HOME/.local/bin/inspire-update" \
    && ok "removed legacy shim $(dim "$HOME/.local/bin/inspire-update")"

  # Make sure ~/.local/bin is on PATH so the user can run `inspire` immediately
  # in the *next* shell. Both uv and pipx put binaries there but neither edits
  # the user's shell rc by default, so a fresh-machine install would leave the
  # user staring at "inspire: command not found".
  if ! command -v inspire >/dev/null 2>&1; then
    case "$INSTALLER" in
      uv)
        if uv tool update-shell >/dev/null 2>&1; then
          ok "added ~/.local/bin to your shell rc via $(bold 'uv tool update-shell')"
        else
          warn "couldn't run $(bold 'uv tool update-shell'); add ~/.local/bin to PATH manually."
        fi
        ;;
      pipx)
        if pipx ensurepath --force >/dev/null 2>&1; then
          ok "added ~/.local/bin to your shell rc via $(bold 'pipx ensurepath')"
        else
          warn "couldn't run $(bold 'pipx ensurepath'); add ~/.local/bin to PATH manually."
        fi
        ;;
    esac
    warn "open a new terminal or run $(bold 'exec \$SHELL') for $(bold inspire) to be on PATH."
  fi

  # Print the version we just landed on, regardless of PATH state. We invoke
  # the binary directly via INSTALLER's known location so the message is
  # accurate even if the user hasn't reloaded their shell yet.
  INSPIRE_BIN=""
  if command -v inspire >/dev/null 2>&1; then
    INSPIRE_BIN="$(command -v inspire)"
  elif [[ -x "$HOME/.local/bin/inspire" ]]; then
    INSPIRE_BIN="$HOME/.local/bin/inspire"
  fi
  if [[ -n "$INSPIRE_BIN" ]]; then
    ok "$(INSPIRE_SKIP_UPDATE_CHECK=1 "$INSPIRE_BIN" --version 2>/dev/null || echo "$PACKAGE installed")"
  else
    die "installed inspire command was not found. Add ~/.local/bin to PATH or rerun the installer."
  fi

  log "preparing Playwright Chromium runtime"
  if INSPIRE_SKIP_UPDATE_CHECK=1 "$INSPIRE_BIN" _ensure-playwright-runtime; then
    ok "Playwright Chromium runtime ready"
  else
    warn "installed CLI has no runtime setup hook yet; using installer-managed setup"
    legacy_playwright_runtime_setup \
      || die "Playwright Chromium runtime setup failed — check network and local browser support, then rerun this installer."
    ok "Playwright Chromium runtime ready"
  fi
fi

# ---- fetch SKILL.md + references/ ------------------------------------------
TMP="$(mktemp -d -t inspire-skill.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

TAR_URL="https://codeload.github.com/${REPO_SLUG}/tar.gz/${DEFAULT_REF}"
log "fetching skill bundle $(dim "$TAR_URL")"
if ! curl -fsSL "$TAR_URL" | tar -xzf - -C "$TMP"; then
  die "tarball fetch failed — check network / proxy and retry."
fi

TOP="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[[ -n "$TOP" && -f "$TOP/SKILL.md" ]] \
  || die "tarball layout unexpected (no SKILL.md under $TOP)."

install_skill() {
  local harness="$1"
  local target
  local legacy_target=""
  case "$harness" in
    claude)   target="$HOME/.claude/skills/inspire"                                    ;;
    codex)    target="$HOME/.codex/skills/inspire"                                     ;;
    antigravity)
      target="$HOME/.gemini/config/skills/inspire"
      legacy_target="$HOME/.gemini/skills/inspire"
      ;;
    cursor)   target="$HOME/.cursor/skills/inspire"                                    ;;
    openclaw) target="$HOME/.openclaw/skills/inspire"                                  ;;
    opencode) target="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}/skills/inspire"   ;;
    qoder)    target="$HOME/.qoder/skills/inspire"                                     ;;
    kimi-code) target="$HOME/.kimi-code/skills/inspire"                                ;;
  esac

  if [[ -n "$legacy_target" && "$legacy_target" != "$target" && ( -L "$legacy_target" || -e "$legacy_target" ) ]]; then
    rm -rf "$legacy_target"
    ok "removed legacy skill path → $(dim "$legacy_target")"
  fi

  # Wipe prior install (handles real dirs and stale symlink layouts).
  if [[ -L "$target" || -e "$target" ]]; then
    rm -rf "$target"
  fi
  mkdir -p "$target"

  cp "$TOP/SKILL.md" "$target/SKILL.md"
  if [[ -d "$TOP/references" ]]; then
    cp -R "$TOP/references" "$target/references"
  fi

  if [[ "$harness" == "codex" ]]; then
    mkdir -p "$target/agents"
    cat >"$target/agents/openai.yaml" <<'YAML'
interface:
  display_name: "Inspire"
  short_description: "Execution-first Inspire operations via the inspire CLI, including auth, proxy routing, notebook/image workflows, and job/HPC execution."
YAML
  fi

  ok "skill → $(dim "$target")"
}

for h in "${HARNESS_LIST[@]}"; do
  install_skill "$h"
done

# ---- schedule background update check (macOS launchd) ----------------------
install_launch_agent() {
  local inspire_path
  inspire_path="$(command -v inspire || true)"
  if [[ -z "$inspire_path" ]]; then
    warn "skipping launchd agent: $(bold inspire) not on PATH."
    return 0
  fi

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
    warn "couldn't load launchd agent (plist written at $plist — run \`launchctl load\` manually)."
  fi
}

if (( INSTALL_SCHEDULE )); then
  case "$(uname -s)" in
    Darwin) install_launch_agent ;;
    *)      warn "automatic update-check scheduling only implemented on macOS; CLI still spawns an opportunistic background check on each use." ;;
  esac
fi

# ---- seed cache so the first invocation prints accurate status -------------
if command -v inspire >/dev/null 2>&1; then
  log "priming update-status cache"
  INSPIRE_SKIP_UPDATE_CHECK=1 inspire update --check --silent || true
fi

echo
bold "InspireSkill installed."
cat <<EOF
  1) Configure accounts & proxy:
        inspire account add <name>
  2) Verify auth and resource visibility:
        inspire config show --compact
        inspire init
        inspire resources availability --workspace all --include-cpu
  3) Check / apply upgrades anytime:
        inspire update --check     # report only
        inspire update             # CLI + SKILL in one shot
EOF
