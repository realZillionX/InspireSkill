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
#   curl -fsSL .../install.sh | bash -s -- --uninstall
#
# Flags:
#   --harness claude[,codex,antigravity,cursor,openclaw,opencode,qoder,qoder-work,
#                    kimi-code,kimi-desktop]
#                                     explicit harness list (default: auto-detect)
#   --no-cli                          skip installing the Python package (skill-only)
#   --no-schedule                     skip the macOS launchd update-check agent
#   --uninstall                       remove everything this script installs
#   --purge                           with --uninstall: also remove ~/.inspire
#   --purge-runtime                   with --uninstall: also remove the shared
#                                     Playwright browser cache
#   --yes                             with --uninstall: skip the confirmation
#
# `inspire uninstall` does the same job from the CLI itself and is the normal
# way to do this. This path exists for a machine where the CLI no longer runs.
#
set -euo pipefail

REPO_SLUG="realZillionX/InspireSkill"
PACKAGE="inspire-skill"
DEFAULT_REF="main"
LAUNCH_LABEL="sh.inspire-skill.update-check"
LAUNCH_LOG="$HOME/Library/Logs/inspire-skill-update-check.log"
ALL_HARNESSES="claude,codex,antigravity,cursor,openclaw,opencode,qoder,qoder-work,kimi-code,kimi-desktop"

HARNESSES=""
INSTALL_CLI=1
INSTALL_SCHEDULE=1
INSTALLER=""
UNINSTALL=0
PURGE=0
PURGE_RUNTIME=0
ASSUME_YES=0
KIMI_CODE_HOME_DIR="${KIMI_CODE_HOME:-$HOME/.kimi-code}"
KIMI_DESKTOP_ROOT="$HOME/Library/Application Support/kimi-desktop/daimon-share/daimon"

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
    --uninstall)     UNINSTALL=1;          shift ;;
    --purge)         PURGE=1;              shift ;;
    --purge-runtime) PURGE_RUNTIME=1;      shift ;;
    -y|--yes)        ASSUME_YES=1;         shift ;;
    -h|--help)       usage ;;
    *)               die "unknown argument: $1" ;;
  esac
done

# ---- shared: where each harness keeps its skill -----------------------------
known_harness() {
  case ",$ALL_HARNESSES," in
    *",$1,"*) return 0 ;;
    *)        return 1 ;;
  esac
}

skill_target() {
  case "$1" in
    claude)       echo "$HOME/.claude/skills/inspire" ;;
    codex)        echo "$HOME/.codex/skills/inspire" ;;
    antigravity)  echo "$HOME/.gemini/config/skills/inspire" ;;
    cursor)       echo "$HOME/.cursor/skills/inspire" ;;
    openclaw)     echo "$HOME/.openclaw/skills/inspire" ;;
    opencode)     echo "${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}/skills/inspire" ;;
    qoder)        echo "$HOME/.qoder/skills/inspire" ;;
    qoder-work)   echo "$HOME/.qoderwork/skills/inspire" ;;
    kimi-code)    echo "$KIMI_CODE_HOME_DIR/skills/inspire" ;;
    kimi-desktop) echo "$KIMI_DESKTOP_ROOT/skills/inspire" ;;
  esac
}

# Home-relative display, so nothing printed here carries the local username.
tildify() {
  case "$1" in
    "$HOME"/*) echo "~${1#"$HOME"}" ;;
    *)         echo "$1" ;;
  esac
}

# ---- uninstall -------------------------------------------------------------
# Mirrors `inspire uninstall`, for a machine whose CLI no longer runs. The two
# share no code, so cli/tests/test_uninstall_command.py pins the constants and
# tiering they both encode.
playwright_cache_dir() {
  # `PLAYWRIGHT_BROWSERS_PATH=0` keeps browsers inside the package, which the
  # package removal already covers.
  [[ "${PLAYWRIGHT_BROWSERS_PATH:-}" == "0" ]] && return 0
  if [[ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]]; then
    echo "$PLAYWRIGHT_BROWSERS_PATH"
  elif [[ "$(uname -s)" == "Darwin" ]]; then
    echo "$HOME/Library/Caches/ms-playwright"
  else
    echo "$HOME/.cache/ms-playwright"
  fi
}

confirm_uninstall() {
  (( ASSUME_YES )) && return 0
  # Under `curl | bash` stdin is the script itself, so the prompt has to come
  # off the terminal directly. `-r /dev/tty` does not answer this: the node
  # exists with read permission even in a session that has no controlling
  # terminal, and only the open fails. Probe it in a subshell so a redirection
  # error on `exec` cannot take this shell down with it.
  ( exec 3<>/dev/tty ) 2>/dev/null \
    || die "no terminal for confirmation — rerun with --yes."
  printf '%s ' "$(bold 'Remove InspireSkill from this machine? [y/N]')" >/dev/tty
  local answer=""
  read -r answer </dev/tty || answer=""
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

remove_package() {
  if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^${PACKAGE} "; then
    uv tool uninstall "$PACKAGE" >/dev/null 2>&1 && return 0
    return 1
  fi
  if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q "^${PACKAGE} "; then
    pipx uninstall "$PACKAGE" >/dev/null 2>&1 && return 0
    return 1
  fi
  return 2
}

do_uninstall() {
  local sweep=() victims=() keeps=() h target plist browsers
  IFS=',' read -r -a sweep <<<"${HARNESSES:-$ALL_HARNESSES}"
  for h in "${sweep[@]}"; do
    known_harness "$h" || die "unknown harness: $h (pick from $ALL_HARNESSES)"
    target="$(skill_target "$h")"
    [[ -n "$target" && ( -e "$target" || -L "$target" ) ]] && victims+=("$target")
  done

  plist="$HOME/Library/LaunchAgents/${LAUNCH_LABEL}.plist"
  [[ -e "$plist" ]] && victims+=("$plist")
  [[ -e "$LAUNCH_LOG" ]] && victims+=("$LAUNCH_LOG")

  if (( PURGE )); then
    [[ -e "$HOME/.inspire" ]] && victims+=("$HOME/.inspire")
  else
    [[ -e "$HOME/.inspire/update-status.json" ]] && victims+=("$HOME/.inspire/update-status.json")
    [[ -e "$HOME/.inspire" ]] && keeps+=("$HOME/.inspire (account config; --purge removes it)")
  fi

  browsers="$(playwright_cache_dir)"
  if [[ -n "$browsers" && -e "$browsers" ]]; then
    if (( PURGE_RUNTIME )); then
      victims+=("$browsers")
    else
      keeps+=("$browsers (shared with other Playwright users; --purge-runtime removes it)")
    fi
  fi

  echo "About to remove:"
  if (( ${#victims[@]} > 0 )); then
    for target in "${victims[@]}"; do printf '  %s\n' "$(tildify "$target")"; done
  fi
  echo "  CLI package: $PACKAGE (if installed via uv or pipx)"
  if (( ${#keeps[@]} > 0 )); then
    echo
    echo "Keeping:"
    for target in "${keeps[@]}"; do printf '  %s\n' "$(tildify "$target")"; done
  fi
  echo

  confirm_uninstall || die "aborted."

  if [[ -e "$plist" ]]; then
    launchctl unload "$plist" >/dev/null 2>&1 || true
  fi
  if (( ${#victims[@]} > 0 )); then
    for target in "${victims[@]}"; do
      rm -rf "$target" || die "could not remove $(tildify "$target")"
      ok "removed $(dim "$(tildify "$target")")"
    done
  fi

  set +e
  remove_package
  local package_status=$?
  set -e
  case "$package_status" in
    0) ok "removed $(bold "$PACKAGE")" ;;
    2) warn "no uv or pipx installation of $(bold "$PACKAGE") found; nothing to remove." ;;
    *) die "could not remove $PACKAGE — uninstall it manually." ;;
  esac

  echo
  bold "InspireSkill uninstalled."
  echo
  exit 0
}

if (( UNINSTALL )); then
  do_uninstall
fi

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
  [[ -d "$HOME/.qoderwork"                                   ]] && found+=("qoder-work")
  [[ -d "$KIMI_CODE_HOME_DIR"                                ]] && found+=("kimi-code")
  [[ -d "$KIMI_DESKTOP_ROOT"                                 ]] && found+=("kimi-desktop")
  (IFS=,; echo "${found[*]:-}")
}

if [[ -z "$HARNESSES" ]]; then
  HARNESSES="$(detect_harnesses)"
  [[ -n "$HARNESSES" ]] \
    || die "no agent harness detected (checked \$HOME/.claude, .codex, .gemini, .cursor, .openclaw, \$OPENCODE_CONFIG_DIR or \$HOME/.config/opencode, .qoder, .qoderwork, \$KIMI_CODE_HOME or \$HOME/.kimi-code, and Kimi Desktop's Application Support directory). Pass --harness explicitly."
  log "auto-detected harnesses: $(bold "$HARNESSES")"
fi

IFS=',' read -r -a HARNESS_LIST <<<"$HARNESSES"
for h in "${HARNESS_LIST[@]}"; do
  known_harness "$h" || die "unknown harness: $h (pick from $ALL_HARNESSES)"
done

# ---- prerequisites ---------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "need '$1' on PATH."; }
need curl
need tar
need mktemp

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
  INSPIRE_SKIP_UPDATE_CHECK=1 "$INSPIRE_BIN" _ensure-playwright-runtime \
    || die "Playwright Chromium runtime setup failed — check network and local browser support, then rerun this installer."
  ok "Playwright Chromium runtime ready"
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
  target="$(skill_target "$harness")"

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
  short_description: "Operate Inspire with focused references and live platform data."
  default_prompt: "Use $inspire to plan and execute this Inspire platform task safely."
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
echo
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
