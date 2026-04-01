"""
ArcGIS Service Manager
System tray app for starting and stopping ArcGIS Enterprise +
ArcGIS Velocity Enterprise Windows services in the correct order.

Must be run as Administrator (service control requires elevated privileges).
"""
import ctypes
import logging
import os
import sys
import threading
import time
from enum import Enum, auto
from typing import List, Optional

import pystray
from PIL import Image, ImageDraw

# Ensure src/ is importable when launched via launch.bat from the project root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from controller import ServiceController
from services import SERVICES


# ---------------------------------------------------------------------------
# Logging — always write to file; also write to stdout when a console exists.
# ---------------------------------------------------------------------------

_LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "arcgis_service_manager.log",
)

_handlers: List[logging.Handler] = [
    logging.FileHandler(_LOG_FILE, encoding="utf-8"),
]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App status
# ---------------------------------------------------------------------------

class AppStatus(Enum):
    UNKNOWN  = auto()
    STOPPED  = auto()
    STARTING = auto()
    RUNNING  = auto()
    STOPPING = auto()
    PARTIAL  = auto()


# (icon fill colour, tray tooltip)
_STATUS_META = {
    AppStatus.UNKNOWN:  ("#808080", "ArcGIS Service Manager — checking…"),
    AppStatus.STOPPED:  ("#cc3333", "ArcGIS Service Manager — all stopped"),
    AppStatus.STARTING: ("#e6a817", "ArcGIS Service Manager — starting…"),
    AppStatus.RUNNING:  ("#2e8b57", "ArcGIS Service Manager — all running"),
    AppStatus.STOPPING: ("#e6a817", "ArcGIS Service Manager — stopping…"),
    AppStatus.PARTIAL:  ("#cc7a00", "ArcGIS Service Manager — partially running"),
}

_STATUS_LABEL = {
    AppStatus.UNKNOWN:  "Checking status…",
    AppStatus.STOPPED:  "All services stopped",
    AppStatus.STARTING: "Starting services…",
    AppStatus.RUNNING:  "All services running",
    AppStatus.STOPPING: "Stopping services…",
    AppStatus.PARTIAL:  "Some services running",
}

_OVERALL_TO_STATUS = {
    "all_running": AppStatus.RUNNING,
    "all_stopped": AppStatus.STOPPED,
    "partial":     AppStatus.PARTIAL,
}


# ---------------------------------------------------------------------------
# Icon image helpers
# ---------------------------------------------------------------------------

def _make_icon_image(color: str, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin - 1, size - margin - 1],
        fill=color,
        outline="#ffffff",
        width=2,
    )
    return img


# ---------------------------------------------------------------------------
# Admin guard
# ---------------------------------------------------------------------------

def _ensure_admin() -> None:
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False

    if not is_admin:
        ctypes.windll.user32.MessageBoxW(
            0,
            "ArcGIS Service Manager must be run as Administrator.\n\n"
            "Right-click launch.bat and choose 'Run as administrator'.",
            "Administrator Required",
            0x10,  # MB_ICONERROR
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class App:
    def __init__(self) -> None:
        self._controller = ServiceController()
        self._status = AppStatus.UNKNOWN
        self._status_lock = threading.Lock()
        self._icon: Optional[pystray.Icon] = None
        self._running = True

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def _get_status(self) -> AppStatus:
        with self._status_lock:
            return self._status

    def _set_status(self, new_status: AppStatus) -> None:
        with self._status_lock:
            if self._status == new_status:
                return
            self._status = new_status

        log.info("Status → %s", new_status.name)

        if self._icon is not None:
            color, title = _STATUS_META[new_status]
            self._icon.icon  = _make_icon_image(color)
            self._icon.title = title
            self._icon.update_menu()

    # ------------------------------------------------------------------
    # Background status polling
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while self._running:
            if not self._controller.busy:
                overall = self._controller.get_overall_status()
                self._set_status(_OVERALL_TO_STATUS.get(overall, AppStatus.UNKNOWN))
            time.sleep(5)

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def _on_start(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if self._controller.busy:
            return
        self._set_status(AppStatus.STARTING)
        log.info("=" * 60)
        log.info("Starting all services")
        log.info("=" * 60)
        self._controller.start_all(on_done=self._on_op_done)

    def _on_stop(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if self._controller.busy:
            return
        self._set_status(AppStatus.STOPPING)
        log.info("=" * 60)
        log.info("Stopping all services")
        log.info("=" * 60)
        self._controller.stop_all(on_done=self._on_op_done)

    def _on_op_done(self, success: bool) -> None:
        if not success:
            log.warning("Operation finished with one or more errors — see log for details.")

    def _on_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if self._controller.busy:
            log.info("Quit requested while operation is in progress — ignored.")
            return
        log.info("Exiting.")
        self._running = False
        icon.stop()

    # ------------------------------------------------------------------
    # Dynamic menu state (called by pystray on each menu render)
    # ------------------------------------------------------------------

    def _menu_status_text(self, item: pystray.MenuItem) -> str:
        return _STATUS_LABEL.get(self._get_status(), "Unknown")

    def _can_start(self, item: pystray.MenuItem) -> bool:
        return (
            self._get_status() in (AppStatus.STOPPED, AppStatus.PARTIAL)
            and not self._controller.busy
        )

    def _can_stop(self, item: pystray.MenuItem) -> bool:
        return (
            self._get_status() in (AppStatus.RUNNING, AppStatus.PARTIAL)
            and not self._controller.busy
        )

    def _can_quit(self, item: pystray.MenuItem) -> bool:
        return not self._controller.busy

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("ArcGIS Service Manager starting (managing %d services).", len(SERVICES))

        poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="status-poll"
        )
        poll_thread.start()

        menu = pystray.Menu(
            pystray.MenuItem(self._menu_status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start All Services", self._on_start, enabled=self._can_start),
            pystray.MenuItem("Stop All Services",  self._on_stop,  enabled=self._can_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit, enabled=self._can_quit),
        )

        initial_color = _STATUS_META[AppStatus.UNKNOWN][0]
        self._icon = pystray.Icon(
            name="arcgis_service_manager",
            icon=_make_icon_image(initial_color),
            title=_STATUS_META[AppStatus.UNKNOWN][1],
            menu=menu,
        )
        self._icon.run()


# ---------------------------------------------------------------------------

def main() -> None:
    _ensure_admin()
    app = App()
    app.run()


if __name__ == "__main__":
    main()
