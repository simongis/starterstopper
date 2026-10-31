"""VMware Workstation via vmrun. No admin rights needed."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Set

from .. import proc
from ..config import Config, System, expand, vmrun_path
from .base import Provider, State

log = logging.getLogger(__name__)


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.strip()))


class VMwareWorkstation(Provider):
    def __init__(self, cfg: Config) -> None:
        self.vmrun = vmrun_path(cfg)

    # -- query --------------------------------------------------------------------
    def running(self) -> Set[str]:
        """Normalised .vmx paths of running VMs (one vmrun call). Suspended VMs are not listed."""
        r = proc.run([self.vmrun, "-T", "ws", "list"], timeout=30)
        if not r.ok:
            raise RuntimeError(f"vmrun list failed: {r.tail(2)}")
        return {_norm(line) for line in r.out.splitlines()[1:] if line.strip()}

    def states(self, systems: List[System]) -> Dict[str, str]:
        try:
            running = self.running()
        except RuntimeError as e:
            log.warning("%s", e)
            return {s.name: State.UNKNOWN for s in systems}
        out = {}
        for s in systems:
            vmx = expand(s.vmx, s)
            if _norm(vmx) in running:
                out[s.name] = State.RUNNING
            elif not Path(vmx).exists():
                out[s.name] = State.MISSING
            elif any(Path(vmx).parent.glob("*.vmss")):
                out[s.name] = State.SUSPENDED
            else:
                out[s.name] = State.STOPPED
        return out

    def resources(self, system: System) -> Dict[str, float]:
        text = Path(expand(system.vmx, system)).read_text(encoding="utf-8", errors="replace")
        mem = re.search(r'^memsize\s*=\s*"(\d+)"', text, re.M)
        cpu = re.search(r'^numvcpus\s*=\s*"(\d+)"', text, re.M)
        return {"memGB": int(mem.group(1)) / 1024 if mem else 0.0, "vcpu": int(cpu.group(1)) if cpu else 1}

    # -- actions ------------------------------------------------------------------
    def preflight(self, system: System) -> str:
        if not Path(self.vmrun).exists() and self.vmrun != "vmrun":
            return f"vmrun not found at {self.vmrun} (set General > vmrun path)"
        vmx = Path(expand(system.vmx, system))
        if not vmx.exists():
            return f"VMX not found: {vmx}"
        return ""

    def ensure_utc_rtc(self, system: System) -> bool:
        """Set rtc.diffFromUTC = "0" in the .vmx (VM must be off). Linux guests read the virtual
        RTC as UTC; without this the guest clock starts offset by the host's UTC offset until NTP
        steps it, and services that start in that window log the wrong time. Returns True if changed."""
        vmx = Path(expand(system.vmx, system))
        lines = vmx.read_text(encoding="utf-8", errors="replace").splitlines()
        if 'rtc.diffFromUTC = "0"' in lines:
            return False
        lines = [ln for ln in lines if not ln.startswith("rtc.diffFromUTC")] + ['rtc.diffFromUTC = "0"']
        vmx.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    def start(self, system: System) -> str:
        vmx = expand(system.vmx, system)
        r = proc.run([self.vmrun, "-T", "ws", "start", vmx, "nogui"], timeout=300)
        if not r.ok:
            return f"vmrun start failed ({r.code}): {r.tail(2)} -- see vmware.log next to the .vmx"
        return ""

    def stop(self, system: System, timeout_s: float) -> str:
        """Soft stop only (guest shutdown via VMware Tools). There is deliberately no hard power-off
        fallback: pulling the plug on a guest is how databases and message logs get corrupted."""
        vmx = expand(system.vmx, system)
        t0 = time.monotonic()
        r = proc.run([self.vmrun, "-T", "ws", "stop", vmx, "soft"], timeout=timeout_s)
        if not r.ok:
            log.warning("%s: vmrun stop returned %s (%s) -- checking whether it stopped anyway",
                        system.name, r.code, r.tail(1))
        while time.monotonic() - t0 < timeout_s:
            try:
                if _norm(vmx) not in self.running():
                    return ""
            except RuntimeError as e:
                log.warning("%s", e)
            time.sleep(5)
        return (f"still running after {timeout_s / 60:.0f} min; left running (no hard power-off). "
                f"Check the guest and vmware.log")

    def show_in_workstation(self, system: System) -> None:
        """Open the VM in the Workstation UI (VMs started by vmrun don't appear in its library)."""
        exe = Path(self.vmrun).with_name("vmware.exe")
        subprocess.Popen([str(exe), "-t", expand(system.vmx, system)], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x8))


def hypervisor_present() -> bool:
    """True if the Windows hypervisor is loaded (Hyper-V/VBS); VMware then runs on a slower path."""
    r = proc.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                  "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent"], timeout=30)
    return r.ok and r.out.strip().lower() == "true"
