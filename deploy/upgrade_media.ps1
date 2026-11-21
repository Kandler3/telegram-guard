param(
    [string]$GuardHome = (Join-Path $env:USERPROFILE '.codex\telegram-local'),
    [string]$SkillHome = (Join-Path $env:USERPROFILE '.codex\skills\telegram-send')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wheelPath = Join-Path $projectRoot 'dist\codex_telegram_guard_local-2.2.0-py3-none-any.whl'
$pythonPath = Join-Path $GuardHome 'venv\Scripts\python.exe'
$packagePath = Join-Path $GuardHome 'venv\Lib\site-packages\telegram_guard_local'
if (!(Test-Path -LiteralPath $wheelPath -PathType Leaf) -or !(Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Build the 2.2.0 wheel and install the existing runtime before upgrading.'
}

# Back up code and skill only. Never inspect/copy account data, whitelist or private state.
$backupPath = Join-Path $projectRoot ('backups\before-2.2.0-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
$codeBackup = Join-Path $backupPath 'telegram_guard_local'
$skillBackup = Join-Path $backupPath 'telegram-send'
New-Item -ItemType Directory -Path $codeBackup,$skillBackup -Force | Out-Null
Get-ChildItem -LiteralPath $packagePath -Filter '*.py' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $codeBackup
}
Copy-Item -LiteralPath (Join-Path $SkillHome 'SKILL.md') -Destination $skillBackup
$existingContent = Join-Path $SkillHome 'references\content.md'
if (Test-Path -LiteralPath $existingContent -PathType Leaf) {
    Copy-Item -LiteralPath $existingContent -Destination (Join-Path $skillBackup 'content.md')
}

& $pythonPath -I -B -m pip install --no-index --no-deps --disable-pip-version-check $wheelPath
if ($LASTEXITCODE -ne 0) { throw 'Runtime upgrade failed; backup is available in the workspace.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\SKILL.md') -Destination (Join-Path $SkillHome 'SKILL.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references\content.md') -Destination $existingContent
& $pythonPath -I -B -c 'import importlib.metadata, telegram_guard_local; assert importlib.metadata.version("codex-telegram-guard-local") == "2.2.0"; assert telegram_guard_local.__version__ == "2.2.0"; print("Installed telegram_guard 2.2.0")'
if ($LASTEXITCODE -ne 0) { throw 'Installed version check failed.' }
Write-Output ('Code/skill backup: ' + $backupPath)
Write-Output 'Restart telegram_guard in Codex Desktop after outstanding sends have completed.'
