"""System providers: how to start, stop and query one kind of system."""
from __future__ import annotations

from typing import Dict

from ..config import Config
from .base import Provider, State
from .vmware_ws import VMwareWorkstation
from .windows_svc import WindowsServices


def providers_for(cfg: Config) -> Dict[str, Provider]:
    """One provider instance per system type."""
    return {"vmware": VMwareWorkstation(cfg), "windows-service": WindowsServices(cfg)}


__all__ = ["Provider", "State", "providers_for", "VMwareWorkstation", "WindowsServices"]
