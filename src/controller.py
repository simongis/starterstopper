"""
ServiceController — start/stop ArcGIS services in the required order
using PowerShell subprocess calls (no pywin32 dependency required).
"""
import logging
import subprocess
import threading
import time
from typing import Callable, Optional

from services import SERVICES

log = logging.getLogger(__name__)

# Suppress the PowerShell console window when the app has no console of its own.
_CREATE_NO_WINDOW = 0x08000000

_PS_STATUS_MAP = {
    "running":         "running",
    "stopped":         "stopped",
    "startpending":    "starting",
    "stoppending":     "stopping",
    "pausepending":    "pausing",
    "paused":          "paused",
    "continuepending": "continuing",
}

_STOPPED_STATES = {"stopped", "not_found"}


def _ps(command: str) -> tuple[int, str, str]:
    """Run a PowerShell one-liner; return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_service_status(name: str) -> str:
    """Return a normalised service state string, or 'not_found' on any error."""
    rc, out, _ = _ps(
        f"(Get-Service -Name '{name}' -ErrorAction SilentlyContinue).Status"
    )
    if not out:
        return "not_found"
    status = _PS_STATUS_MAP.get(out.lower().replace(" ", ""), "unknown")
    if status == "unknown":
        log.debug("Unrecognised service state %r for %r", out, name)
    return status


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
        statuses = [get_service_status(svc.service_name) for svc in SERVICES]
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
                if get_service_status(svc.service_name) == "running":
                    self._emit(on_progress, f"[skip]    {svc.name} — already running")
                    continue

                self._emit(on_progress, f"[start]   {svc.name}")
                rc, out, err = _ps(f"Start-Service -Name '{svc.service_name}'")
                if rc != 0:
                    self._emit(on_progress, f"[error]   {svc.name}: {err or out}")
                    success = False
                    break

                if not _wait_for_state(svc.service_name, "running"):
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
                if get_service_status(svc.service_name) in _STOPPED_STATES:
                    self._emit(on_progress, f"[skip]    {svc.name} — already stopped")
                    continue

                self._emit(on_progress, f"[stop]    {svc.name}")
                rc, out, err = _ps(f"Stop-Service -Name '{svc.service_name}' -Force")
                if rc != 0:
                    self._emit(on_progress, f"[error]   {svc.name}: {err or out}")
                    success = False
                    # Keep going — attempt to stop remaining services anyway.
                    continue

                if not _wait_for_state(svc.service_name, "stopped"):
                    self._emit(on_progress, f"[timeout] {svc.name} did not reach 'stopped' within 5 min")
                    success = False
        finally:
            with self._lock:
                self._busy = False
            if on_done:
                on_done(success)
