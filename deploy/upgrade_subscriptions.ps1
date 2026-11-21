param(
    [string]$GuardHome = (Join-Path $env:USERPROFILE '.codex\telegram-local'),
    [string]$SkillHome = (Join-Path $env:USERPROFILE '.codex\skills\telegram-send')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wheelPath = Join-Path $projectRoot 'dist\codex_telegram_guard_local-2.3.0-py3-none-any.whl'
$pythonPath = Join-Path $GuardHome 'venv\Scripts\python.exe'
$packagePath = Join-Path $GuardHome 'venv\Lib\site-packages\telegram_guard_local'
if (!(Test-Path -LiteralPath $wheelPath -PathType Leaf)) { throw 'Build the 2.3.0 wheel first.' }
$backupPath = Join-Path $projectRoot ('backups\before-2.3.0-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
$codeBackup = Join-Path $backupPath 'telegram_guard_local'
$skillBackup = Join-Path $backupPath 'telegram-send'
New-Item -ItemType Directory -Path $codeBackup,$skillBackup -Force | Out-Null
Get-ChildItem -LiteralPath $packagePath -Filter '*.py' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $codeBackup
}
Copy-Item -LiteralPath (Join-Path $SkillHome 'SKILL.md') -Destination $skillBackup
Copy-Item -LiteralPath (Join-Path $SkillHome 'references') -Destination $skillBackup -Recurse
& $pythonPath -I -B -m pip install --no-index --find-links (Join-Path $projectRoot 'dist') --disable-pip-version-check $wheelPath
if ($LASTEXITCODE -ne 0) { throw 'Runtime upgrade failed; backup remains available.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\SKILL.md') -Destination (Join-Path $SkillHome 'SKILL.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references\subscriptions.md') -Destination (Join-Path $SkillHome 'references\subscriptions.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'deploy\shared-desktop.ps1') -Destination (Join-Path $GuardHome 'shared-desktop.ps1')
if (!(Test-Path -LiteralPath (Join-Path $GuardHome 'shared-desktop.json'))) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'deploy\shared-desktop.json') -Destination (Join-Path $GuardHome 'shared-desktop.json')
}
& $pythonPath -I -B -c 'import importlib.metadata, telegram_guard_local; assert importlib.metadata.version("codex-telegram-guard-local") == "2.3.0"; assert telegram_guard_local.__version__ == "2.3.0"; import websockets; print("Installed telegram_guard 2.3.0; websockets",websockets.__version__)'
if ($LASTEXITCODE -ne 0) { throw 'Installed version check failed.' }
Write-Output ('Code/skill backup: ' + $backupPath)
Write-Output 'Desktop remains running. Close it normally before using shared-desktop.ps1 -Action Start.'

