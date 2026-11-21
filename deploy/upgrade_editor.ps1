param(
    [string]$GuardHome = (Join-Path $env:USERPROFILE '.codex\telegram-local'),
    [string]$SkillHome = (Join-Path $env:USERPROFILE '.codex\skills\telegram-send')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wheelPath = Join-Path $projectRoot 'dist\codex_telegram_guard_local-2.4.0-py3-none-any.whl'
$pythonPath = Join-Path $GuardHome 'venv\Scripts\python.exe'
$packagePath = Join-Path $GuardHome 'venv\Lib\site-packages\telegram_guard_local'
if (!(Test-Path -LiteralPath $wheelPath -PathType Leaf)) { throw 'Build the 2.4.0 wheel first.' }
$backupPath = Join-Path $projectRoot ('backups\before-2.4.0-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
Copy-Item -LiteralPath $packagePath -Destination (Join-Path $backupPath 'telegram_guard_local') -Recurse
Copy-Item -LiteralPath $SkillHome -Destination (Join-Path $backupPath 'telegram-send') -Recurse
& $pythonPath -I -B -m pip install --no-index --no-deps --disable-pip-version-check $wheelPath
if ($LASTEXITCODE -ne 0) { throw 'Runtime upgrade failed; backup remains available.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'deploy\telegram-whitelist.ps1') -Destination (Join-Path $GuardHome 'telegram-whitelist.ps1')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\SKILL.md') -Destination (Join-Path $SkillHome 'SKILL.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references\whitelist-editor.md') -Destination (Join-Path $SkillHome 'references\whitelist-editor.md')
& $pythonPath -I -B -c 'import importlib.metadata, telegram_guard_local; from telegram_guard_local.whitelist_editor import ASSETS; assert importlib.metadata.version("codex-telegram-guard-local") == "2.4.0"; assert telegram_guard_local.__version__ == "2.4.0"; assert all((ASSETS / p).is_file() for p in ("index.html","app.js","style.css")); print("Installed telegram_guard 2.4.0 and browser editor assets")'
if ($LASTEXITCODE -ne 0) { throw 'Installed version check failed.' }
Write-Output ('Code/skill backup: ' + $backupPath)
Write-Output 'No Desktop restart needed for the standalone editor. Whitelist, private state and global config were not changed.'
