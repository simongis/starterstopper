"""Entry point.

    pythonw StarterStopper.pyw            tray (no console)
    pythonw StarterStopper.pyw --prefs    Preferences window
    python  StarterStopper.pyw --cli start|stop|health|status [names...]
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR


def _logging(console: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list = [RotatingFileHandler(LOG_DIR / "starterstopper.log", maxBytes=1_000_000, backupCount=3,
                                          encoding="utf-8")]
    if console and sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", handlers=handlers)


def _single_instance() -> bool:
    """Named mutex: False if another tray instance is already running."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\StarterStopperTray")
    return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS


def _cli(op: str, names: list) -> int:
    from . import config, health
    from .engine import Engine
    cfg = config.load()
    eng = Engine(cfg, on_progress=lambda m: None)
    sel = names or None
    if op == "status":
        for name, st in eng.states().items():
            print(f"{name:30} {st}")
        return 0
    if op == "start":
        res = eng.start(sel)
        print(res.summary())
        rc = 0 if res.verdict == "READY" else 1
        if cfg.health.runAfterStart and sel is None and res.verdict != "CANCELLED":
            rep = health.run_checks(cfg, on_progress=print, extra=[
                health.CheckItem(source=f"startup/{s}", check=c, status=st, detail=d) for s, c, st, d in res.findings])
            print(rep.summary(), "->", health.save_report(rep)[1])
            rc = rc or (0 if rep.verdict in ("HEALTHY", "NO CHECKS") else 1)
        return rc
    if op == "stop":
        res = eng.stop(sel)
        print(res.summary())
        return 0 if res.verdict == "STOPPED" else 1
    if op == "health":
        rep = health.run_checks(cfg, on_progress=print)
        print(rep.summary(), "->", health.save_report(rep)[1])
        return 0 if rep.verdict in ("HEALTHY", "NO CHECKS") else 1
    return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="StarterStopper")
    ap.add_argument("--prefs", action="store_true", help="open Preferences")
    ap.add_argument("--cli", choices=["start", "stop", "health", "status"], help="run once in the console")
    ap.add_argument("names", nargs="*", help="system names (default: all enabled)")
    a = ap.parse_args(argv)

    if a.prefs:
        from . import prefs
        return prefs.main()
    _logging(console=bool(a.cli))
    if a.cli:
        return _cli(a.cli, a.names)
    if not _single_instance():
        ctypes.windll.user32.MessageBoxW(0, "StarterStopper is already running (see the system tray).",
                                         "StarterStopper", 0x40)
        return 0
    from .tray import App
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
