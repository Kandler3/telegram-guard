$ErrorActionPreference = 'Stop'
$codexDirectory = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$settingsPath = Join-Path $codexDirectory 'telegram-local\shared-desktop.json'
if (-not (Test-Path -LiteralPath $settingsPath)) { throw 'Existing shared Desktop configuration was not found.' }
$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'agent-wake-runtime.ps1')
$nodeExecutable = Get-AgentWakeNodeRuntime -ConfiguredNode $settings.node
$entry = Join-Path $PSScriptRoot 'cli.mjs'
& $nodeExecutable $entry @args
exit $LASTEXITCODE
