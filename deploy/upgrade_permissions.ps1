param(
    [string]$GuardHome = (Join-Path $env:USERPROFILE '.codex\telegram-local'),
    [string]$SkillHome = (Join-Path $env:USERPROFILE '.codex\skills\telegram-send')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wheelPath = Join-Path $projectRoot 'dist\codex_telegram_guard_local-2.5.0-py3-none-any.whl'
$pythonPath = Join-Path $GuardHome 'venv\Scripts\python.exe'
$packagePath = Join-Path $GuardHome 'venv\Lib\site-packages\telegram_guard_local'
if (!(Test-Path -LiteralPath $wheelPath -PathType Leaf)) { throw 'Build the 2.5.0 wheel first.' }
$backupPath = Join-Path $projectRoot ('backups\before-2.5.0-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
Copy-Item -LiteralPath $packagePath -Destination (Join-Path $backupPath 'telegram_guard_local') -Recurse
Copy-Item -LiteralPath $SkillHome -Destination (Join-Path $backupPath 'telegram-send') -Recurse
& $pythonPath -I -B -m pip install --no-index --no-deps --disable-pip-version-check $wheelPath
if ($LASTEXITCODE -ne 0) { throw 'Runtime upgrade failed; backup remains available.' }
& $pythonPath -I -B -m telegram_guard_local.policy_migration --home $GuardHome
if ($LASTEXITCODE -ne 0) { throw 'Policy migration failed. Do not claim read/write activation.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\SKILL.md') -Destination (Join-Path $SkillHome 'SKILL.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references\permissions.md') -Destination (Join-Path $SkillHome 'references\permissions.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references\content.md') -Destination (Join-Path $SkillHome 'references\content.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references\subscriptions.md') -Destination (Join-Path $SkillHome 'references\subscriptions.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'skills\telegram-send\references\whitelist-editor.md') -Destination (Join-Path $SkillHome 'references\whitelist-editor.md')
& $pythonPath -I -B -c 'import importlib.metadata; assert importlib.metadata.version("codex-telegram-guard-local") == "2.5.0"; print("Installed telegram_guard 2.5.0; restart MCP to load read/write enforcement.")'
if ($LASTEXITCODE -ne 0) { throw 'Installed version check failed.' }
Write-Output ('Code/skill backup: ' + $backupPath)
Write-Output 'Original policy backup stays only in the protected Guard directory. Account credentials and Telegram session were not read.'
