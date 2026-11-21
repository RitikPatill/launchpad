"""
Human-facing alerts. launchpad once failed silently for 39 days because
nothing surfaced failures to a human. Every degraded or failed state now
goes through notify(), which:
  1. logs to the DB (state.log),
  2. appends to launchpad_alert.md on the Desktop (next to overseer's
     autopilot_*.md files),
  3. pops a Windows toast notification (best effort, never raises).

Alerts are de-duplicated per `key` with a cooldown so a persistent
problem nags at most every ALERT_COOLDOWN_HOURS instead of every tick.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import state
from config import LOCAL_HOME

ALERT_COOLDOWN_HOURS = 12
_STATE_FILE = LOCAL_HOME / "alerts_state.json"
_onedrive_desktop = Path.home() / "OneDrive" / "Desktop"
DESKTOP = _onedrive_desktop if _onedrive_desktop.exists() else Path.home() / "Desktop"
ALERT_FILE = DESKTOP / "launchpad_alert.md"


def _load() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass


_TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
# The script is constant: title/body arrive via environment variables, so no
# LLM-written text can ever be interpreted as PowerShell (any quote style).
_TOAST_PS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
    "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$t = $x.GetElementsByTagName('text');"
    "$t.Item(0).AppendChild($x.CreateTextNode($env:LP_TOAST_TITLE)) > $null;"
    "$t.Item(1).AppendChild($x.CreateTextNode($env:LP_TOAST_BODY)) > $null;"
    "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LP_TOAST_APP).Show($n)"
)


def _toast(title: str, body: str, *, wait: bool = False) -> int | None:
    if sys.platform != "win32":
        return None
    env = {**os.environ, "LP_TOAST_TITLE": title[:120], "LP_TOAST_BODY": body.replace("\n", " ")[:240],
           "LP_TOAST_APP": _TOAST_APP_ID}
    try:
        p = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.wait(timeout=60) if wait else None
    except Exception:
        return None


def notify(key: str, title: str, body: str, *, level: str = "WARN",
           cooldown_hours: float = ALERT_COOLDOWN_HOURS) -> bool:
    """Raise an alert unless the same key fired within the cooldown.
    Returns True if the alert was actually raised. The DB log is best-effort:
    a full disk or a wedged DB must not stop the desktop file and toast."""
    try:
        state.log(level, "alerts", f"{title}: {body}")
    except Exception:
        pass
    seen = _load()
    now = datetime.now(timezone.utc)
    last = seen.get(key)
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=cooldown_hours):
                return False
        except ValueError:
            pass
    seen[key] = now.isoformat()
    _save(seen)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        header = "" if ALERT_FILE.exists() else (
            "# launchpad alerts\n\n_Newest at the bottom. Safe to delete this file; "
            "it is recreated on the next alert._\n\n")
        with ALERT_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{header}- **{stamp} [{level}] {title}** - {body}\n")
    except Exception:
        pass
    _toast(f"launchpad: {title}", body)
    return True


def clear(key: str) -> None:
    """Forget a key so the next occurrence alerts immediately."""
    seen = _load()
    if seen.pop(key, None) is not None:
        _save(seen)
