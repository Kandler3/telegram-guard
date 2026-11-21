# Read-only Node discovery. Never edits Desktop settings or changes permissions.
function Test-AgentWakeNodeRuntime {
    param([string]$Executable)
    if (-not [IO.Path]::IsPathRooted($Executable) -or
        -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    $probe = "const c=await import('node:crypto'); if(typeof WebSocket!=='function'||typeof c.createHmac!=='function'||typeof c.randomBytes!=='function'||typeof c.timingSafeEqual!=='function')process.exit(1);"
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = '--input-type=module --eval "' + $probe + '"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { return $false }
        if (-not $process.WaitForExit(5000)) {
            $process.Kill() # Only this capability probe, never a worker or Desktop.
            [void]$process.WaitForExit(1000)
            return $false
        }
        return $process.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        $process.Dispose()
    }
}

function Get-AgentWakeNodeRuntime {
    param(
        [string]$ConfiguredNode,
        [string]$LocalAppData = $env:LOCALAPPDATA,
        [string]$UserProfile = $env:USERPROFILE
    )
    $candidates = New-Object 'System.Collections.Generic.List[string]'
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredNode)) { $candidates.Add($ConfiguredNode) }
    if (-not [string]::IsNullOrWhiteSpace($LocalAppData)) {
        $runtimeRoot = Join-Path $LocalAppData 'OpenAI\Codex\runtimes\cua_node'
        if (Test-Path -LiteralPath $runtimeRoot -PathType Container) {
            $runtimeFiles = @(Get-ChildItem -LiteralPath $runtimeRoot -Directory -ErrorAction SilentlyContinue |
                ForEach-Object {
                    $path = Join-Path $_.FullName 'bin\node.exe'
                    if (Test-Path -LiteralPath $path -PathType Leaf) { Get-Item -LiteralPath $path }
                } | Sort-Object LastWriteTimeUtc, FullName -Descending)
            foreach ($file in $runtimeFiles) { $candidates.Add($file.FullName) }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($UserProfile)) {
        $candidates.Add((Join-Path $UserProfile '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'))
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($candidate in $candidates) {
        if (-not [IO.Path]::IsPathRooted($candidate) -or -not $seen.Add($candidate)) { continue }
        if (Test-AgentWakeNodeRuntime -Executable $candidate) { return $candidate }
    }
    throw 'No compatible Codex Node runtime was found. Required capabilities: WebSocket and node:crypto. The Desktop and its settings were not changed.'
}
