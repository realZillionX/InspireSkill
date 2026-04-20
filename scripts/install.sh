#!/usr/bin/env bash
# InspireSkill user installer — no local clone, no symlinks.
#
# Reads: none (self-contained tarball + uv/pipx download)
# Writes:
#   - ~/.local/bin/inspire       (uv tool / pipx shim; installer-managed)
#   - ~/.{claude,codex,gemini}/skills/inspire/{SKILL.md, references/, ...}
#   - ~/Library/LaunchAgents/sh.inspire-skill.update-check.plist  (macOS only)
#   - ~/.inspire/update-status.json  (via post-install `inspire update --check`)
#
# Usage (typical, no clone required):
#   curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --harness claude,codex
#   curl -fsSL .../install.sh | bash -s -- --no-schedule
#
# Flags:
#   --harness claude[,codex,gemini]   explicit harness list (default: auto-detect)
#   --no-cli                          skip installing the Python package (skill-only)
#   --no-schedule                     skip the macOS launchd update-check agent
#   --ref <git-ref>                   pin install/refresh to a branch/tag/SHA (default: main)
#
# Developers working on this repo should use scripts/install-dev.sh instead.

set -euo pipefail

REPO_SLUG="realZillionX/InspireSkill"
PACKAGE="inspire-skill"
DEFAULT_REF="main"
LAUNCH_LABEL="sh.inspire-skill.update-check"

HARNESSES=""
INSTALL_CLI=1
INSTALL_SCHEDULE=1
REF="$DEFAULT_REF"

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

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness)       HARNESSES="$2";       shift 2 ;;
    --harness=*)     HARNESSES="${1#*=}";  shift ;;
    --no-cli)        INSTALL_CLI=0;        shift ;;
    --no-schedule)   INSTALL_SCHEDULE=0;   shift ;;
    --ref)           REF="$2";             shift 2 ;;
    --ref=*)         REF="${1#*=}";        shift ;;
    -h|--help)       usage ;;
    *)               die "unknown argument: $1" ;;
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
  [[ -n "$HARNESSES" ]] \
    || die "no agent harness detected (checked \$HOME/.claude, .codex, .gemini, .openclaw, and \$OPENCODE_CONFIG_DIR or \$HOME/.config/opencode). Pass --harness explicitly."
  log "auto-detected harnesses: $(bold "$HARNESSES")"
fi

IFS=',' read -r -a HARNESS_LIST <<<"$HARNESSES"
for h in "${HARNESS_LIST[@]}"; do
  case "$h" in
    claude|codex|gemini|openclaw|opencode) ;;
    *) die "unknown harness: $h (pick from claude,codex,gemini,openclaw,opencode)" ;;
  esac
done

# ---- prerequisites ---------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "need '$1' on PATH."; }
need curl
need tar
need mktemp

# ---- install CLI via uv tool / pipx ----------------------------------------
SPEC="git+https://github.com/${REPO_SLUG}.git@${REF}#subdirectory=cli"

if (( INSTALL_CLI )); then
  if command -v uv >/dev/null 2>&1; then
    log "installing $(bold "$PACKAGE") via $(bold 'uv tool') from $(dim "$SPEC")"
    uv tool install --force "$SPEC"
  elif command -v pipx >/dev/null 2>&1; then
    log "installing $(bold "$PACKAGE") via $(bold pipx) from $(dim "$SPEC")"
    pipx install --force "$SPEC"
  else
    die "need uv or pipx. Install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi

  if command -v inspire >/dev/null 2>&1; then
    ok "$(inspire --version 2>/dev/null || echo "$PACKAGE installed")"
  else
    warn "$(bold inspire) not on PATH yet. If you use uv tool, run $(bold 'uv tool update-shell') or restart your shell."
  fi

  # Clean up stale symlinks from previous dev installs / legacy Inspire-cli shims.
  for stale in "$HOME/.local/bin/inspire-update"; do
    if [[ -L "$stale" ]]; then
      rm -f "$stale"
      ok "removed legacy shim $(dim "$stale")"
    fi
  done
fi

# ---- fetch SKILL.md + references/ ------------------------------------------
TMP="$(mktemp -d -t inspire-skill.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

TAR_URL="https://codeload.github.com/${REPO_SLUG}/tar.gz/refs/heads/${REF}"
log "fetching skill bundle $(dim "$TAR_URL")"
if ! curl -fsSL "$TAR_URL" | tar -xzf - -C "$TMP"; then
  die "tarball fetch failed — check network / proxy and that ref '$REF' exists."
fi

TOP="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[[ -n "$TOP" && -f "$TOP/SKILL.md" ]] \
  || die "tarball layout unexpected (no SKILL.md under $TOP)."

install_skill() {
  local harness="$1"
  local target
  case "$harness" in
    claude)   target="$HOME/.claude/skills/inspire"                                    ;;
    codex)    target="$HOME/.codex/skills/inspire"                                     ;;
    gemini)   target="$HOME/.gemini/skills/inspire"                                    ;;
    openclaw) target="$HOME/.openclaw/skills/inspire"                                  ;;
    opencode) target="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}/skills/inspire"   ;;
  esac

  # Wipe prior install (handles both real-dir and dev-mode symlink layouts).
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
        inspire init
  2) Verify auth and resource visibility:
        inspire config show --compact
        inspire resources list --all --include-cpu
  3) Check / apply upgrades anytime:
        inspire update --check     # report only
        inspire update             # CLI + SKILL in one shot
EOF
