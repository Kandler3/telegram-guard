param(
    [string]$GuardHome = (Join-Path $env:USERPROFILE '.codex\telegram-local'),
    [string]$SkillHome = (Join-Path $env:USERPROFILE '.codex\skills\telegram-send'),
    [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wheelPath = Join-Path $projectRoot 'dist\codex_telegram_guard_local-2.6.0-py3-none-any.whl'
$pythonPath = Join-Path $GuardHome 'venv\Scripts\python.exe'
$packagePath = Join-Path $GuardHome 'venv\Lib\site-packages\telegram_guard_local'
if (!(Test-Path -LiteralPath $wheelPath -PathType Leaf)) { throw 'Build the 2.6.0 wheel first.' }
$backupPath = Join-Path $projectRoot ('backups\before-2.6.0-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
Copy-Item -LiteralPath $packagePath -Destination (Join-Path $backupPath 'telegram_guard_local') -Recurse
Copy-Item -LiteralPath $SkillHome -Destination (Join-Path $backupPath 'telegram-send') -Recurse
& $pythonPath -I -B -m pip install --force-reinstall --no-index --no-deps --disable-pip-version-check $wheelPath
if ($LASTEXITCODE -ne 0) { throw 'Runtime upgrade failed; code/skill backup remains available.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\SKILL.md') -Destination (Join-Path $SkillHome 'SKILL.md')
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references') -Filter '*.md' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $SkillHome ('references\' + $_.Name))
}
& $pythonPath -I -B (Join-Path $PSScriptRoot 'configure_streaming.py') --codex-home $CodexHome --apply
if ($LASTEXITCODE -ne 0) { throw 'Code installed but config update failed; do not claim activation.' }
& $pythonPath -I -B (Join-Path $PSScriptRoot 'verify_streaming_install.py')
if ($LASTEXITCODE -ne 0) { throw 'Installed version check failed.' }
Write-Output ('Code/skill backup: ' + $backupPath)
Write-Output 'Whitelist, credentials and Telegram session were not read or changed by this installer. Desktop remains running.'
