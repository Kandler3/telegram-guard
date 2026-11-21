param([ValidateSet('Check','Start','Stop')][string]$Action = 'Check')
$ErrorActionPreference = 'Stop'
$guardDirectory = Join-Path $env:USERPROFILE '.codex\telegram-local'
$pythonExecutable = Join-Path $guardDirectory 'venv\Scripts\python.exe'
& $pythonExecutable -I -B -m telegram_guard_local.shared_desktop --home $guardDirectory --action $Action.ToLowerInvariant()
if ($LASTEXITCODE -ne 0) { throw 'Shared Desktop action failed. The current Desktop was not closed by this launcher.' }

