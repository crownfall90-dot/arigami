param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Создайте окружение: python -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt' }
New-Item -ItemType Directory -Force -Path .runtime | Out-Null
$pidFile = Join-Path $PSScriptRoot '.runtime\collector.pid'
$collectorRunning = $false
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($savedPid -match '^\d+$' -and (Get-Process -Id ([int]$savedPid) -ErrorAction SilentlyContinue)) {
        $collectorRunning = $true
    }
}
if (-not $collectorRunning) {
    $collector = Start-Process -FilePath $python -ArgumentList '-m','bot.live' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput "$PSScriptRoot\.runtime\collector.log" -RedirectStandardError "$PSScriptRoot\.runtime\collector-error.log" -PassThru
    $collector.Id | Set-Content $pidFile
}
& $python -m bot --serve --port $Port
