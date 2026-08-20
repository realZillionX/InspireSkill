<#
.SYNOPSIS
    InspireSkill installer for Windows — published package plus managed skill files.

.DESCRIPTION
    The Windows counterpart to scripts/install.sh, and it installs the same way:
    the published `inspire-skill` package through `uv tool` or `pipx`, never an
    editable checkout. That matters beyond tidiness — `inspire update` decides
    whether it can upgrade itself by looking for `uv\tools` or `pipx\venvs` in
    sys.prefix, and an editable install has neither, so it silently loses the
    ability to update.

    Skill files are laid down by the CLI itself, which already knows every
    harness directory and auto-detects the installed ones. Duplicating that list
    here would just give it somewhere to drift out of sync.

    Writes:
      - the `inspire` shim (uv tool / pipx; typically %USERPROFILE%\.local\bin)
      - skill directories for every detected harness, e.g. ~\.claude\skills\inspire
      - %USERPROFILE%\.inspire\update-status.json

    There is no scheduled background update check, which is the one thing the
    macOS installer sets up that this does not. It is not needed: the CLI spawns
    its own detached check when the cached status goes stale.

.PARAMETER SkipPlaywright
    Skip the Chromium download. Browser login will not work until you run
    `inspire update --cli-only`.

.PARAMETER SkipSkill
    Install the CLI only, and leave harness skill directories alone.

.PARAMETER Version
    Install an exact version instead of the latest release.

.EXAMPLE
    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\install.ps1

.NOTES
    Requires Python 3.10+, plus `uv` or `pipx`. Install uv with:
        powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

    The Windows OpenSSH Client is required for `notebook ssh`, `notebook exec`,
    `notebook scp` and `job logs --transport ssh`:
        Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
#>
[CmdletBinding()]
param(
    [switch]$SkipPlaywright,
    [switch]$SkipSkill,
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$Package = 'inspire-skill'
$Spec = if ($Version) { "$Package==$Version" } else { $Package }

function Write-Step { param([string]$Message) Write-Host "› $Message" -ForegroundColor Blue }
function Write-Ok   { param([string]$Message) Write-Host "✓ $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "! $Message" -ForegroundColor Yellow }

# The console has to be able to render the CLI's own output — box drawing,
# arrows, Chinese workspace names. On a zh-CN console that means leaving cp936.
if ($Host.Name -eq 'ConsoleHost') {
    try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }
}

# ---- install the CLI -------------------------------------------------------

$uv = Get-Command uv -ErrorAction SilentlyContinue
$pipx = Get-Command pipx -ErrorAction SilentlyContinue

if ($uv) {
    $installer = 'uv'
    Write-Step "installing $Spec via uv tool"
    & uv tool install --force --refresh $Spec
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed for '$Spec'." }

    # A leftover pipx install would put a second `inspire` shim on PATH and the
    # two would race for the same name.
    if ($pipx) {
        $installed = & pipx list --short 2>$null
        if ($LASTEXITCODE -eq 0 -and ($installed -match "^$([regex]::Escape($Package)) ")) {
            Write-Step "removing earlier pipx install of $Package (uv tool now owns it)"
            & pipx uninstall $Package 2>&1 | Out-Null
        }
    }
} elseif ($pipx) {
    $installer = 'pipx'
    Write-Step "installing $Spec via pipx"
    & pipx install --force $Spec
    if ($LASTEXITCODE -ne 0) { throw "pipx install failed for '$Spec'." }
} else {
    throw @'
Need uv or pipx on PATH. Install uv with:
    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
'@
}

# ---- make sure the shim is reachable ---------------------------------------

$inspire = Get-Command inspire -ErrorAction SilentlyContinue
if (-not $inspire) {
    switch ($installer) {
        'uv'   { & uv tool update-shell 2>&1 | Out-Null }
        'pipx' { & pipx ensurepath --force 2>&1 | Out-Null }
    }
    # update-shell / ensurepath edit the persisted user PATH, which this session
    # will not see. Look where both installers place their shims so the rest of
    # this script can still run the CLI it just installed.
    foreach ($candidate in @(
        (Join-Path $HOME '.local\bin\inspire.exe'),
        (Join-Path $env:USERPROFILE '.local\bin\inspire.exe')
    )) {
        if (Test-Path $candidate) { $inspireExe = $candidate; break }
    }
    if (-not $inspireExe) {
        throw 'Installed inspire command was not found. Open a new terminal and rerun this script.'
    }
    Write-Warn 'open a new terminal for `inspire` to be on PATH.'
} else {
    $inspireExe = $inspire.Source
}

$env:INSPIRE_SKIP_UPDATE_CHECK = '1'
$installedVersion = (& $inspireExe --version) -replace '^\D*', ''
Write-Ok "inspire $installedVersion"

# ---- browser runtime -------------------------------------------------------

if (-not $SkipPlaywright) {
    Write-Step 'preparing Playwright Chromium runtime'
    & $inspireExe _ensure-playwright-runtime
    if ($LASTEXITCODE -ne 0) {
        throw 'Playwright Chromium runtime setup failed — check network access, then rerun this script.'
    }
    Write-Ok 'Playwright Chromium runtime ready'
}

# ---- skill files -----------------------------------------------------------

if (-not $SkipSkill) {
    Write-Step 'installing skill files for detected harnesses'
    & $inspireExe _refresh-skills
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'skill refresh reported a problem; rerun `inspire update` once the CLI is on PATH.'
    } else {
        Write-Ok 'skill files installed'
    }
}

# ---- seed the cache so the first invocation prints accurate status ---------

& $inspireExe update --check --silent 2>&1 | Out-Null

Remove-Item Env:\INSPIRE_SKIP_UPDATE_CHECK -ErrorAction SilentlyContinue

Write-Host ''
Write-Ok 'InspireSkill installed.'
Write-Host ''
Write-Host '  1) Configure accounts & proxy:'
Write-Host '        inspire account add <name>'
Write-Host '  2) Verify auth and resource visibility:'
Write-Host '        inspire config show --compact'
Write-Host '        inspire init'
Write-Host '        inspire resources availability --workspace all --include-cpu'
Write-Host '  3) Check / apply upgrades anytime:'
Write-Host '        inspire update --check     # report only'
Write-Host '        inspire update             # CLI + SKILL in one shot'
