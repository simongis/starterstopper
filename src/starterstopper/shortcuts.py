"""Keep the Startup-folder shortcut in step with General > launchAtLogin."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import proc
from .config import APP_DIR

log = logging.getLogger(__name__)
NAME = "StarterStopper.lnk"


def startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _pythonw() -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else exe)


def create(path: Path) -> None:
    pyw = APP_DIR / "StarterStopper.pyw"
    icon = APP_DIR / "assets" / "starterstopper.ico"
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SS_LNK);"
        "$s.TargetPath = $env:SS_TARGET; $s.Arguments = $env:SS_ARGS;"
        "$s.WorkingDirectory = $env:SS_DIR; $s.IconLocation = $env:SS_ICON;"
        "$s.Description = 'StarterStopper - start/stop/health-check your lab'; $s.Save()"
    )
    env = {"SS_LNK": str(path), "SS_TARGET": _pythonw(), "SS_ARGS": f'"{pyw}"', "SS_DIR": str(APP_DIR),
           "SS_ICON": f"{icon},0" if icon.exists() else f"{_pythonw()},0"}
    r = proc.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30, env=env)
    if not r.ok:
        raise RuntimeError(r.tail(2))


def sync_startup(enabled: bool) -> None:
    lnk = startup_dir() / NAME
    if enabled and not lnk.exists():
        create(lnk)
        log.info("Created Startup shortcut %s", lnk)
    elif not enabled and lnk.exists():
        lnk.unlink()
        log.info("Removed Startup shortcut %s", lnk)
