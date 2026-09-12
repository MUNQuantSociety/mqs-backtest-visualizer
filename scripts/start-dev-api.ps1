param(
    [int]$Port = 8000,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Backend virtualenv missing: $python"
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Stop the existing backend before starting another."
}

# A reloader can retain the listening socket after its application child dies.
# Keep the default server in this terminal so Ctrl+C stops it and native crash
# traces remain visible. Background execution is an explicit opt-in.
$arguments = @('-X', 'faulthandler', '-u', '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', "$Port", '--log-level', 'info')
if (-not $Background) {
    Push-Location -LiteralPath $projectRoot
    try {
        & $python @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Backend exited with code $LASTEXITCODE. See the traceback above."
        }
    } finally {
        Pop-Location
    }
    return
}

$logDirectory = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logDirectory "backend-$stamp.log"
$stderr = Join-Path $logDirectory "backend-$stamp.err.log"
$backendProcess = Start-Process -FilePath $python -ArgumentList $arguments `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$deadline = (Get-Date).AddSeconds(30)
do {
    $backendProcess.Refresh()
    if ($backendProcess.HasExited) {
        throw "Backend exited with code $($backendProcess.ExitCode). See $stderr and $stdout"
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
        if ($health.status -eq 'ok') {
            [pscustomobject]@{
                Status = 'Ready'
                ProcessId = $backendProcess.Id
                Url = "http://127.0.0.1:$Port"
                OutputLog = $stdout
                ErrorLog = $stderr
            }
            return
        }
    } catch {
        # The socket may not be bound while imports and database startup finish.
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

throw "Backend did not pass its health check within 30 seconds (PID $($backendProcess.Id)). See $stderr and $stdout"
