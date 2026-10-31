"""The resident tray app. Kept small: pystray + Pillow + stdlib only (no tkinter -- Preferences is
a separate short-lived process). Idle cost is one batched status query every idlePollSeconds."""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pystray
from PIL import Image, ImageDraw

from . import config as cfgmod
from . import health, shortcuts
from .config import APP_DIR, CONFIG_PATH, LOG_DIR, Config, ConfigError
from .engine import Engine, OpResult
from .providers import State
from .providers.vmware_ws import hypervisor_present

log = logging.getLogger(__name__)

COLOURS = {
    "ready": "#2e8b57", "degraded": "#e08a00", "partial": "#e08a00", "stopped": "#c0392b",
    "busy": "#e6c117", "unknown": "#808080", "empty": "#808080",
}


def _icon(colour: str, warn: bool = False, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, size - 5, size - 5], fill=colour, outline="#ffffff", width=3)
    if warn:  # small amber/black badge: hypervisor loaded or config error
        d.ellipse([size - 26, 0, size - 1, 25], fill="#ffcc00", outline="#000000", width=2)
        d.rectangle([size - 15, 5, size - 12, 15], fill="#000000")
        d.rectangle([size - 15, 18, size - 12, 21], fill="#000000")
    return img


class App:
    def __init__(self) -> None:
        self.cfg = Config()
        self.cfg_error = ""
        self.cfg_mtime = 0.0
        self.states: Dict[str, str] = {}
        self.busy = ""                      # '' or a description of the running operation
        self.progress = ""
        self.engine: Optional[Engine] = None
        self.last_op: Optional[OpResult] = None
        self.last_health: Optional[health.HealthReport] = health.load_last()
        self.hypervisor = False
        self.icon: Optional[pystray.Icon] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._icons: Dict[tuple, Image.Image] = {}
        self.reload_config(force=True)

    # ------------------------------------------------------------------ config
    def reload_config(self, force: bool = False) -> bool:
        """Reload if the file changed. Returns True if a (re)load happened."""
        try:
            mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
        except OSError:
            mtime = 0.0
        if not force and mtime == self.cfg_mtime:
            return False
        if self.busy:                       # never swap config under a running operation
            return False
        self.cfg_mtime = mtime
        try:
            self.cfg = cfgmod.load()
            self.cfg_error = ""
            log.info("Config loaded: %d system(s) from %s", len(self.cfg.systems), CONFIG_PATH)
        except ConfigError as e:
            self.cfg_error = str(e)
            log.error("Config error: %s", e)
            self.notify("Config error", str(e))
        self.engine = Engine(self.cfg, on_progress=self._on_progress)
        try:
            shortcuts.sync_startup(self.cfg.general.launchAtLogin)
        except Exception as e:
            log.warning("Could not update the Startup shortcut: %s", e)
        self.refresh_icon()
        return True

    # ------------------------------------------------------------------ status
    def overall(self) -> str:
        if self.busy:
            return "busy"
        systems = self.cfg.enabled_systems()
        if not systems:
            return "empty"
        st = [self.states.get(s.name, State.UNKNOWN) for s in systems]
        if any(x == State.UNKNOWN for x in st):
            return "unknown"
        if all(x == State.RUNNING for x in st):
            degraded = (self.last_health is not None and self.last_health.verdict in ("DEGRADED", "ERROR")) or \
                       (self.last_op is not None and self.last_op.op == "start" and self.last_op.verdict != "READY")
            return "degraded" if degraded else "ready"
        if all(x in (State.STOPPED, State.SUSPENDED, State.MISSING) for x in st):
            return "stopped"
        return "partial"

    def status_text(self) -> str:
        o = self.overall()
        if o == "busy":
            return f"{self.busy}: {self.progress}"[:120] if self.progress else f"{self.busy}..."
        n = len(self.cfg.enabled_systems())
        up = sum(1 for s in self.cfg.enabled_systems() if self.states.get(s.name) == State.RUNNING)
        return {
            "ready": f"All {n} running",
            "degraded": f"All {n} running -- degraded (see health report)",
            "partial": f"{up} of {n} running",
            "stopped": "All stopped",
            "unknown": "Checking status...",
            "empty": "No systems configured -- open Preferences",
        }[o]

    def refresh_icon(self) -> None:
        if not self.icon:
            return
        o = self.overall()
        warn = self.hypervisor or bool(self.cfg_error)
        key = (o, warn)
        if key not in self._icons:
            self._icons[key] = _icon(COLOURS[o], warn)
        self.icon.icon = self._icons[key]
        title = f"StarterStopper -- {self.status_text()}"
        if self.cfg_error:
            title += " [config error]"
        self.icon.title = title[:127]       # Windows tooltip limit
        self.icon.update_menu()

    def poll_once(self) -> None:
        self.reload_config()
        if self.engine:
            self.states = self.engine.states()
        self.refresh_icon()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("status poll failed")
            g = self.cfg.general
            self._wake.wait(g.busyPollSeconds if self.busy else g.idlePollSeconds)
            self._wake.clear()

    def _on_progress(self, msg: str) -> None:
        self.progress = msg
        self.refresh_icon()

    def notify(self, title: str, msg: str) -> None:
        if self.icon and self.cfg.general.notifications:
            try:
                self.icon.notify(msg[:250], title[:60])
            except Exception:
                pass

    # ------------------------------------------------------------------ operations
    def _run_op(self, label: str, fn: Callable[[], None]) -> None:
        if self.busy or not self.engine:
            return
        self.busy, self.progress = label, ""
        self.refresh_icon()
        self._wake.set()

        def worker() -> None:
            try:
                fn()
            except Exception as e:
                log.exception("%s failed", label)
                self.notify(f"{label} failed", str(e))
            finally:
                self.busy, self.progress = "", ""
                self._wake.set()
                self.refresh_icon()

        threading.Thread(target=worker, daemon=True, name=label).start()

    def start(self, names: Optional[List[str]] = None) -> None:
        def op() -> None:
            assert self.engine
            log.info("=" * 70)
            log.info("START %s", ", ".join(names) if names else "all")
            if self.cfg.general.checkHypervisor and any(s.type == "vmware" for s in self.cfg.systems):
                self.hypervisor = hypervisor_present()
                if self.hypervisor:
                    log.warning("Windows hypervisor is loaded: VMware will run on the slower path")
            res = self.engine.start(names)
            self.last_op = res
            log.info("START result: %s (%.0fs)", res.summary(), res.seconds)
            if res.verdict != "CANCELLED" and self.cfg.health.runAfterStart and names is None:
                self.progress = "running health checks"
                self.refresh_icon()
                self._health(extra=res)
                verdict = "READY" if res.verdict == "READY" and self.last_health and \
                    self.last_health.verdict in ("HEALTHY", "NO CHECKS") else "DEGRADED"
                if res.verdict == "FAILED":
                    verdict = "FAILED"
                self.notify(f"Start: {verdict}", self.last_health.summary() if self.last_health else res.summary())
            else:
                self.notify(f"Start: {res.verdict}", res.summary())
        self._run_op("Starting", op)

    def stop(self, names: Optional[List[str]] = None) -> None:
        def op() -> None:
            assert self.engine
            log.info("=" * 70)
            log.info("STOP %s", ", ".join(names) if names else "all")
            res = self.engine.stop(names)
            self.last_op = res
            log.info("STOP result: %s (%.0fs)", res.summary(), res.seconds)
            self.notify(f"Stop: {res.verdict}", res.summary())
        self._run_op("Stopping", op)

    def health_check(self) -> None:
        def op() -> None:
            log.info("HEALTH CHECK")
            assert self.engine
            res = OpResult("health", "")
            self.engine.info_probes(self.cfg.enabled_systems(), res)
            self._health(extra=res)
            if self.last_health:
                self.notify(f"Health: {self.last_health.verdict}", self.last_health.summary())
        self._run_op("Health check", op)

    def _health(self, extra: OpResult) -> None:
        items = [health.CheckItem(source=f"startup/{s}", check=c, status=st, detail=d)
                 for (s, c, st, d) in extra.findings]
        cancel = self.engine.cancel_event if self.engine else None
        rep = health.run_checks(self.cfg, on_progress=self._on_progress, cancel=cancel, extra=items)
        _json, html = health.save_report(rep)
        self.last_health = rep
        log.info("HEALTH result: %s -> %s", rep.summary(), html)

    def cancel(self) -> None:
        if self.engine and self.busy:
            log.warning("Cancel requested")
            self.engine.cancel()

    # ------------------------------------------------------------------ helpers
    def open_prefs(self) -> None:
        _spawn([str(APP_DIR / "StarterStopper.pyw"), "--prefs"])

    def open_report(self) -> None:
        p = health.REPORT_DIR / "last.html"
        if p.exists():
            os.startfile(str(p))
        else:
            self.notify("No health report yet", "Run a health check first.")

    def show_in_workstation(self, name: str) -> None:
        if self.engine:
            prov = self.engine.providers["vmware"]
            prov.show_in_workstation(self.cfg.system(name))  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ menu
    def _menu(self):
        M, I, SEP = pystray.Menu, pystray.MenuItem, pystray.Menu.SEPARATOR
        idle = lambda _i: not self.busy and not self.cfg_error  # noqa: E731

        def items():
            yield I(lambda _i: self.status_text(), None, enabled=False)
            if self.last_health:
                yield I(lambda _i: f"Health: {self.last_health.summary()}"[:110], self.open_report_cb)
            if self.hypervisor:
                yield I("Warning: Windows hypervisor loaded (VMware slowed)", None, enabled=False)
            if self.cfg_error:
                yield I(f"Config error: {self.cfg_error}"[:110], self.prefs_cb)
            yield SEP
            yield I("Start all", lambda: self.start(), enabled=idle)
            yield I("Stop all", lambda: self.stop(), enabled=idle)
            if self.busy:
                yield I("Cancel (stops waiting; never powers off)", lambda: self.cancel())
            if self.cfg.systems:
                yield I("Systems", M(lambda: self._system_items()))
            yield SEP
            yield I("Run health check", lambda: self.health_check(), enabled=idle)
            yield I("Open last health report", self.open_report_cb)
            if self.cfg.links:
                yield I("Open", M(*[I(l.label or l.url, self._link_cb(l.url)) for l in self.cfg.links]))
            yield SEP
            yield I("Preferences...", self.prefs_cb)
            yield I("Open log", lambda: os.startfile(str(LOG_DIR / "starterstopper.log")))
            yield I("Quit", self.quit_cb, enabled=lambda _i: not self.busy)

        return M(items)

    def _system_items(self):
        I, M = pystray.MenuItem, pystray.Menu
        out = []
        for s in self.cfg.systems:
            st = self.states.get(s.name, State.UNKNOWN)
            label = f"{s.name}  ({st}{'' if s.enabled else ', not in Start all'})"
            sub = [I("Start", self._cb(self.start, [s.name]), enabled=lambda _i: not self.busy),
                   I("Stop", self._cb(self.stop, [s.name]), enabled=lambda _i: not self.busy)]
            if s.type == "vmware":
                sub.append(I("Show in VMware Workstation", self._cb(self.show_in_workstation, s.name)))
            out.append(I(label, M(*sub)))
        return out

    @staticmethod
    def _cb(fn, arg):
        return lambda: fn(arg)

    @staticmethod
    def _link_cb(url: str):
        return lambda: os.startfile(url)

    def open_report_cb(self) -> None:
        self.open_report()

    def prefs_cb(self) -> None:
        self.open_prefs()

    def quit_cb(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.icon:
            self.icon.stop()

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self.icon = pystray.Icon("StarterStopper", _icon(COLOURS["unknown"]), "StarterStopper", self._menu())

        def setup(icon: pystray.Icon) -> None:
            icon.visible = True
            threading.Thread(target=self._poll_loop, daemon=True, name="poll").start()
            threading.Thread(target=self._startup_checks, daemon=True, name="startup").start()

        self.icon.run(setup=setup)

    def _startup_checks(self) -> None:
        if self.cfg.general.checkHypervisor and any(s.type == "vmware" for s in self.cfg.systems):
            self.hypervisor = hypervisor_present()
            if self.hypervisor:
                self.notify("Windows hypervisor is loaded",
                            "VMware Workstation will run on the slower Hyper-V path. Check VBS / Hyper-V features.")
            self.refresh_icon()
        if self.cfg.general.startAllAtLogin and _uptime_s() < 15 * 60 and not self.cfg_error:
            log.info("startAllAtLogin: host booted %.0f min ago -- starting all", _uptime_s() / 60)
            time.sleep(30)                  # let the desktop and network settle
            self.start()


def _uptime_s() -> float:
    return ctypes.windll.kernel32.GetTickCount64() / 1000.0


def _spawn(args: List[str]) -> None:
    """Launch a detached pythonw child (Preferences) with no console."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe)] + args, cwd=str(APP_DIR),
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS, close_fds=True)
