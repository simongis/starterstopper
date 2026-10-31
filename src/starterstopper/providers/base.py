from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List

from ..config import System


class State:
    RUNNING = "running"
    STOPPED = "stopped"
    SUSPENDED = "suspended"     # VMware: has a .vmss; start resumes it
    PARTIAL = "partial"         # some of a system's services running
    MISSING = "missing"         # .vmx / service not found
    UNKNOWN = "unknown"


class Provider(ABC):
    """Start/stop/query for one system type. Calls block; the engine runs them off the UI thread."""

    @abstractmethod
    def states(self, systems: List[System]) -> Dict[str, str]:
        """Batched state for many systems in as few process launches as possible: {name: State}."""

    @abstractmethod
    def start(self, system: System) -> str:
        """Begin starting. Return '' on success, else an error message. Readiness is the engine's job."""

    @abstractmethod
    def stop(self, system: System, timeout_s: float) -> str:
        """Stop gracefully and confirm it is stopped within timeout_s. Return '' or an error message."""

    def preflight(self, system: System) -> str:
        """Checks before start (paths exist, etc). Return '' or an error message."""
        return ""

    def resources(self, system: System) -> Dict[str, float]:
        """{'memGB': x, 'vcpu': n} if known; used for the start ceiling."""
        return {}
