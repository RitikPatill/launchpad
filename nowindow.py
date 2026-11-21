"""
Global child-process policy (Windows): never show a window, never hog the CPU.

The owner's rules: launchpad must NEVER pop up a visible window (no console
flashes from claude / ffmpeg / PowerShell / voice engines, no browser
windows), and it shares the laptop with the owner's daytime work.

Importing this module (orchestrator.py does it first) patches
subprocess.Popen so every child process launchpad starts - directly, or via
libraries such as imageio-ffmpeg and Playwright's driver - gets
CREATE_NO_WINDOW plus BELOW_NORMAL_PRIORITY_CLASS unless the caller chose
its own creationflags. Grandchildren (e.g. Chromium under Playwright's
driver) inherit the below-normal priority class. Browsers are additionally
always launched headless by the renderer/recorder.
"""
from __future__ import annotations

import subprocess
import sys

_PATCHED = False


def install() -> None:
    global _PATCHED
    if _PATCHED or sys.platform != "win32":
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | \
        getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    original_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = flags
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init
    _PATCHED = True


install()
