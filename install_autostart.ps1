# Registers launchpad as a Windows Task Scheduler entry. Heartbeat
# offset 10 min so the four orchestrators alternate cleanly.
#
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunBat    = Join-Path $ScriptDir "run.bat"

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c start /min `"`" `"$RunBat`""

$LogonTrigger    = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$PeriodicTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(8).AddMinutes(10) `
                       -RepetitionInterval (New-TimeSpan -Minutes 30) `
                       -RepetitionDuration (New-TimeSpan -Days 9999)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -WakeToRun

Register-ScheduledTask -TaskName "launchpad" `
    -Action $Action -Trigger $LogonTrigger, $PeriodicTrigger `
    -Principal $Principal -Settings $Settings `
    -Description "launchpad autonomous YouTube uploader (Sonnet only)" `
    -Force | Out-Null

Write-Host "Task 'launchpad' registered."
