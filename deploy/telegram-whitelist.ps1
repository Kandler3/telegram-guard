param([ValidateLength(0, 200)][string]$Search = '')
$ErrorActionPreference = 'Stop'
$taskArgs = @('-I', '-B', '-m', 'telegram_guard_local.whitelist_editor', '--home', $PSScriptRoot)
if ($Search) { $taskArgs += @('--search', $Search) }
& (Join-Path $PSScriptRoot 'venv\Scripts\python.exe') @taskArgs
exit $LASTEXITCODE
