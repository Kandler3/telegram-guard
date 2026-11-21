param([string]$GuardHome = (Join-Path $env:USERPROFILE '.codex\telegram-local'))
$ErrorActionPreference = 'Stop'
$pythonWindowless = Join-Path $GuardHome 'venv\Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) { throw 'Windowless Python runtime is missing.' }
$configuration = Get-Content -LiteralPath (Join-Path $GuardHome 'shared-desktop.json') -Raw | ConvertFrom-Json
$entryPath = Join-Path $GuardHome 'start-codex.pyw'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'start-codex.pyw') -Destination $entryPath
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcutPath = Join-Path $folder 'Codex + Telegram.lnk'
    if (Test-Path -LiteralPath $shortcutPath) {
        $existingShortcut = $shell.CreateShortcut($shortcutPath)
        if ($existingShortcut.TargetPath -ne $pythonWindowless) { throw 'An unrelated shortcut already has this name.' }
    }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonWindowless
    $shortcut.Arguments = '-I -B "' + $entryPath + '"'
    $shortcut.WorkingDirectory = $GuardHome
    $shortcut.IconLocation = $configuration.desktop + ',0'
    $shortcut.Description = 'Codex с фоновыми подписками Telegram, без консоли'
    $shortcut.Save()
    $verified = $shell.CreateShortcut($shortcutPath)
    if ($verified.TargetPath -ne $pythonWindowless -or $verified.Arguments -ne $shortcut.Arguments) { throw 'Shortcut verification failed.' }
    Write-Output $shortcutPath
}
