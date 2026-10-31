"""Readiness / info probes: HTTP(S) from this host, or a command run in the guest over SSH."""
from __future__ import annotations

import os
import shutil
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from . import proc
from .config import Probe, System, expand


@dataclass
class ProbeResult:
    ok: bool
    detail: str          # e.g. "200", "503", "timeout", "exit 1: ..."


_INSECURE = ssl.create_default_context()
_INSECURE.check_hostname = False
_INSECURE.verify_mode = ssl.CERT_NONE


def http_probe(p: Probe, system: System) -> ProbeResult:
    url = expand(p.http or "", system)
    ctx = None if p.verifyTls else _INSECURE
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "StarterStopper"})
    try:
        with urllib.request.urlopen(req, timeout=p.timeoutSeconds, context=ctx) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # URLError, timeout, connection reset, TLS errors
        reason = getattr(e, "reason", e)
        short = {"ConnectionRefusedError": "refused", "ConnectionResetError": "reset",
                 "TimeoutError": "timeout", "timeout": "timeout", "gaierror": "DNS failed",
                 "SSLError": "TLS error", "SSLCertVerificationError": "TLS cert untrusted"}
        name = type(reason).__name__
        return ProbeResult(False, short.get(name) or f"{name}: {reason}"[:120])
    return ProbeResult(code in p.expect, str(code))


_WIN_SSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"


def ssh_exe() -> Tuple[str, str]:
    """(ssh executable, null device it understands). Prefer Windows OpenSSH: a POSIX build on PATH
    (Git for Windows, MSYS2) would treat 'NUL' as a filename and write known_hosts into it."""
    if _WIN_SSH.exists():
        return str(_WIN_SSH), "NUL"
    found = shutil.which("ssh") or "ssh"
    return found, ("NUL" if "openssh" in found.lower() else "/dev/null")


def ssh_base(system: System, connect_timeout: int = 5) -> List[str]:
    """ssh argv up to (and including) user@host. BatchMode: never prompt for a password."""
    exe, null = ssh_exe()
    args = [exe, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={null}", "-o", "LogLevel=ERROR",
            "-p", str(system.ssh.port)]
    if system.ssh.keyPath:
        args += ["-i", expand(system.ssh.keyPath, system)]
    target = f"{system.ssh.user}@{system.host}" if system.ssh.user else system.host
    return args + [target]


def ssh_run(system: System, command: str, timeout: float = 30) -> proc.Result:
    return proc.run(ssh_base(system) + [expand(command, system)], timeout=timeout)


def ssh_probe(p: Probe, system: System) -> ProbeResult:
    r = ssh_run(system, p.ssh or "", timeout=p.timeoutSeconds + 10)
    if r.ok:
        return ProbeResult(True, "exit 0")
    return ProbeResult(False, f"exit {r.code}" + (f": {r.tail(1)}" if r.tail(1) else ""))


def run_probe(p: Probe, system: System) -> ProbeResult:
    return http_probe(p, system) if p.http else ssh_probe(p, system)


def ssh_reachable(system: System) -> bool:
    return ssh_run(system, "true", timeout=15).ok
