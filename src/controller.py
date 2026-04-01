"""
ServiceController — start/stop ArcGIS services in the required order,
waiting between each step so dependent services have time to initialise.
"""
import logging
import threading
import time
from typing import Callable, List, Optional

import pywintypes
import win32service
import win32serviceutil

from services import SERVICES

log = logging.getLogger(__name__)

_STATE_MAP = {
    win32service.SERVICE_RUNNING:          "running",
    win32service.SERVICE_STOPPED:          "stopped",
    win32service.SERVICE_START_PENDING:    "starting",
    win32service.SERVICE_STOP_PENDING:     "stopping",
    win32service.SERVICE_PAUSE_PENDING:    "pausing",
    win32service.SERVICE_PAUSED:           "paused",
    win32service.SERVICE_CONTINUE_PENDING: "continuing",
}

_STOPPED_STATES = {"stopped", "not_found"}


def get_service_status(name: str) -> str:
    """Return a human-readable service state, or 'not_found' on error."""
    try:
        status = win32serviceutil.QueryServiceStatus(name)
        return _STATE_MAP.get(status[1], "unknown")
    except pywintypes.error as exc:
        log.debug("QueryServiceStatus(%r): %s", name, exc)
        return "not_found"


def _wait_for_state(
    name: str,
    target: str,
    timeout: int = 300,
    poll: float = 2.0,
) -> bool:
    """Poll until the service reaches *target* state or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if get_service_status(name) == target:
            return True
        time.sleep(poll)
    return False


class ServiceController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def get_overall_status(self) -> str:
        """Return 'all_running', 'all_stopped', or 'partial'."""
        statuses = [get_service_status(svc.name) for svc in SERVICES]
        if all(s == "running" for s in statuses):
            return "all_running"
        if all(s in _STOPPED_STATES for s in statuses):
            return "all_stopped"
        return "partial"

    # ------------------------------------------------------------------
    # Public async launchers
    # ------------------------------------------------------------------

    def start_all(
        self,
        on_progress: Optional[Callable[[str], None]] = None,
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> threading.Thread:
        """Start all services in defined order. Returns immediately (background thread)."""
        t = threading.Thread(
            target=self._run_start,
            args=(on_progress, on_done),
            daemon=True,
            name="start-all",
        )
        t.start()
        return t

    def stop_all(
        self,
        on_progress: Optional[Callable[[str], None]] = None,
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> threading.Thread:
        """Stop all services in reverse order. Returns immediately (background thread)."""
        t = threading.Thread(
            target=self._run_stop,
            args=(on_progress, on_done),
            daemon=True,
            name="stop-all",
        )
        t.start()
        return t

    # ------------------------------------------------------------------
    # Internal workers
    # ------------------------------------------------------------------

    def _emit(self, fn: Optional[Callable[[str], None]], msg: str) -> None:
        log.info(msg)
        if fn:
            fn(msg)

    def _run_start(
        self,
        on_progress: Optional[Callable[[str], None]],
        on_done: Optional[Callable[[bool], None]],
    ) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True

        success = True
        try:
            for svc in SERVICES:
                if get_service_status(svc.name) == "running":
                    self._emit(on_progress, f"[skip]    {svc.name} — already running")
                    continue

                self._emit(on_progress, f"[start]   {svc.name}")
                try:
                    win32serviceutil.StartService(svc.name)
                except pywintypes.error as exc:
                    self._emit(on_progress, f"[error]   {svc.name}: {exc}")
                    success = False
                    break

                if not _wait_for_state(svc.name, "running"):
                    self._emit(on_progress, f"[timeout] {svc.name} did not reach 'running' within 5 min")
                    success = False
                    break

                if svc.wait_after_start > 0:
                    self._emit(
                        on_progress,
                        f"[wait]    {svc.name} — stabilising for {svc.wait_after_start}s…",
                    )
                    time.sleep(svc.wait_after_start)
        finally:
            with self._lock:
                self._busy = False
            if on_done:
                on_done(success)

    def _run_stop(
        self,
        on_progress: Optional[Callable[[str], None]],
        on_done: Optional[Callable[[bool], None]],
    ) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True

        success = True
        try:
            for svc in reversed(SERVICES):
                if get_service_status(svc.name) in _STOPPED_STATES:
                    self._emit(on_progress, f"[skip]    {svc.name} — already stopped")
                    continue

                self._emit(on_progress, f"[stop]    {svc.name}")
                try:
                    win32serviceutil.StopService(svc.name)
                except pywintypes.error as exc:
                    self._emit(on_progress, f"[error]   {svc.name}: {exc}")
                    success = False
                    # Keep going — attempt to stop remaining services anyway.
                    continue

                if not _wait_for_state(svc.name, "stopped"):
                    self._emit(on_progress, f"[timeout] {svc.name} did not reach 'stopped' within 5 min")
                    success = False
        finally:
            with self._lock:
                self._busy = False
            if on_done:
                on_done(success)
