@echo off
REM Run the orchestrator loop. Used by Task Scheduler and for manual launches.
REM
REM stdout/stderr MUST be redirected to a file. When invoked headless via the
REM hidden VBS launcher, an un-redirected python process attached to a hidden
REM conhost buffer eventually blocks on stdout writes and the process dies —
REM that's what stalled launchpad on 12 May, autodev on 9 May, agent-radar
REM on 7 May. Redirecting to a file makes the autostart path identical to
REM running it from a real terminal.
cd /d "%~dp0"
if not exist "%LOCALAPPDATA%\launchpad" mkdir "%LOCALAPPDATA%\launchpad"
python orchestrator.py run 1>>"%LOCALAPPDATA%\launchpad\autostart.log" 2>&1
