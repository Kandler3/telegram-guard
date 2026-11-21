param([string]$GuardHome = (Join-Path $env:USERPROFILE '.codex\telegram-local'))
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wheelPath = Join-Path $projectRoot 'dist\codex_telegram_guard_local-2.6.2-py3-none-any.whl'
$pythonPath = Join-Path $GuardHome 'venv\Scripts\python.exe'
$packagePath = Join-Path $GuardHome 'venv\Lib\site-packages\telegram_guard_local'
if (!(Test-Path -LiteralPath $wheelPath -PathType Leaf)) { throw 'Build the 2.6.2 wheel first.' }
$backupPath = Join-Path $projectRoot ('backups\before-2.6.2-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
Copy-Item -LiteralPath $packagePath -Destination (Join-Path $backupPath 'telegram_guard_local') -Recurse
& $pythonPath -I -B -m pip install --force-reinstall --no-index --no-deps --disable-pip-version-check $wheelPath
if ($LASTEXITCODE -ne 0) { throw 'Runtime installation failed; backup remains available.' }
& $pythonPath -I -B (Join-Path $PSScriptRoot 'repair_launcher.py') --home $GuardHome
if ($LASTEXITCODE -ne 0) { throw 'Runtime installed but launcher configuration was not updated.' }
$adapterPath = Join-Path $GuardHome 'venv\Scripts\codex-shared-stdio.exe'
if (!(Test-Path -LiteralPath $adapterPath -PathType Leaf)) { throw 'Desktop stdio adapter was not installed.' }
$adapterVersion = (& $adapterPath --version | Out-String).Trim()
$expectedVersion = (Get-Content -LiteralPath (Join-Path $GuardHome 'shared-desktop.json') -Raw | ConvertFrom-Json).version
if ($LASTEXITCODE -ne 0 -or $adapterVersion -ne $expectedVersion) { throw 'Installed adapter CLI version delegation failed.' }
Write-Output $adapterVersion
Write-Output ('Code backup: ' + $backupPath)
Write-Output 'Installed for the next normal launch. Running Desktop/server, access policy and credentials were not changed.'
