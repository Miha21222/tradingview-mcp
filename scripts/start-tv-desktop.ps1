# Launch TradingView Desktop (Microsoft Store install) with CDP enabled for the
# opt-in `desktop` toolset. Resolves the WindowsApps path at runtime so app
# updates (which change the versioned folder name) don't break the launcher.
#
# Usage:  powershell -File scripts\start-tv-desktop.ps1 [-Port 9223]
#
# Default port is 9223 (NOT the conventional 9222): on this machine wmux's
# browser panel already listens on 9222, and Chromium silently skips binding a
# busy port - the app then runs with no CDP at all. Set TV_CDP_URL to match.

param([int]$Port = 9223)

$pkg = Get-AppxPackage TradingView.Desktop
if (-not $pkg) {
    Write-Error "TradingView Desktop (Store app) is not installed. Install it from the Microsoft Store or https://www.tradingview.com/desktop/"
    exit 1
}

$exe = Join-Path $pkg.InstallLocation "TradingView.exe"
Start-Process $exe -ArgumentList "--remote-debugging-port=$Port"

Start-Sleep -Seconds 6
$ok = (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -WarningAction SilentlyContinue).TcpTestSucceeded
if ($ok) {
    Write-Host "TradingView Desktop running; CDP listening on 127.0.0.1:$Port"
} else {
    Write-Warning "App launched but port $Port is not listening yet - it may need a few more seconds, or another instance without the flag is already running (close it first)."
}
