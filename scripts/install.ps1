[CmdletBinding()]
param(
    [switch]$SkipPlaywright,
    [switch]$SkipSkill
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$cliRoot = Join-Path $repoRoot 'cli'

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $pythonExe = 'py'
    $pythonArgs = @('-3')
} elseif ($pythonCommand) {
    $pythonExe = 'python'
    $pythonArgs = @()
} else {
    throw 'Python 3.10 or newer was not found. Install it from python.org and ensure py.exe or python.exe is on PATH.'
}

& $pythonExe @pythonArgs --version | Out-Host
& $pythonExe @pythonArgs -m pip install --upgrade pip | Out-Host
& $pythonExe @pythonArgs -m pip install -e $cliRoot | Out-Host

if (-not $SkipPlaywright) {
    & $pythonExe @pythonArgs -m playwright install chromium | Out-Host
}

if (-not $SkipSkill) {
    $skillTarget = Join-Path $HOME '.codex\skills\inspire'
    New-Item -ItemType Directory -Force -Path $skillTarget | Out-Null
    Copy-Item (Join-Path $repoRoot 'SKILL.md') (Join-Path $skillTarget 'SKILL.md') -Force
    $referencesTarget = Join-Path $skillTarget 'references'
    New-Item -ItemType Directory -Force -Path $referencesTarget | Out-Null
    Copy-Item (Join-Path $repoRoot 'references\*') $referencesTarget -Recurse -Force
    $agentDir = Join-Path $skillTarget 'agents'
    New-Item -ItemType Directory -Force -Path $agentDir | Out-Null
    @'
interface:
  display_name: "Inspire"
  short_description: "Operate Inspire with focused references and live platform data."
  default_prompt: "Use $inspire to plan and execute this Inspire platform task safely."
'@ | Set-Content -Path (Join-Path $agentDir 'openai.yaml') -Encoding utf8
}

Write-Host ''
Write-Host 'InspireSkill installed for Windows.'
Write-Host 'Next: inspire account add <name>'
