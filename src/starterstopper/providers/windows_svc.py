"""Windows services on this PC. Status is read unelevated through the Service Control Manager API
(no process launch). Start/stop need admin, so they run one hidden, elevated PowerShell per action:
one UAC prompt per start or stop, and none if the app itself is already elevated."""
from __future__ import annotations

import ctypes
import logging
import os
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List

from .. import proc
from ..config import Config, System
from .base import Provider, State

log = logging.getLogger(__name__)

_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SERVICE_RUNNING = 0x00000004
_SERVICE_STOPPED = 0x00000001
_ERROR_SERVICE_DOES_NOT_EXIST = 1060


class _SERVICE_STATUS(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
        "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint")]


def _service_states(names: List[str]) -> Dict[str, str]:
    """{service_name: 'running'|'stopped'|'pending'|'missing'|'unknown'} without admin rights."""
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenSCManagerW.restype = wintypes.HANDLE
    adv.OpenServiceW.restype = wintypes.HANDLE
    adv.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    adv.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(_SERVICE_STATUS)]
    adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    out: Dict[str, str] = {}
    scm = adv.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if not scm:
        return {n: "unknown" for n in names}
    try:
        for n in names:
            h = adv.OpenServiceW(scm, n, _SERVICE_QUERY_STATUS)
            if not h:
                out[n] = "missing" if ctypes.get_last_error() == _ERROR_SERVICE_DOES_NOT_EXIST else "unknown"
                continue
            st = _SERVICE_STATUS()
            ok = adv.QueryServiceStatus(h, ctypes.byref(st))
            adv.CloseServiceHandle(h)
            if not ok:
                out[n] = "unknown"
            elif st.dwCurrentState == _SERVICE_RUNNING:
                out[n] = "running"
            elif st.dwCurrentState == _SERVICE_STOPPED:
                out[n] = "stopped"
            else:
                out[n] = "pending"
    finally:
        adv.CloseServiceHandle(scm)
    return out


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def _run_elevated_ps(script: str, timeout_s: float) -> str:
    """Run a PowerShell script hidden and elevated; wait for it. Return '' or an error message.
    Output goes to a temp file because an elevated child can't share our pipes."""
    tmpdir = Path(tempfile.mkdtemp(prefix="starterstopper-"))
    ps1, logf = tmpdir / "op.ps1", tmpdir / "op.log"
    ps1.write_text("$ErrorActionPreference = 'Stop'\n"
                   f"Start-Transcript -LiteralPath '{logf}' | Out-Null\n"
                   f"try {{\n{script}\n}} catch {{ Write-Output \"ERROR: $($_.Exception.Message)\"; exit 1 }}"
                   " finally { Stop-Transcript | Out-Null }\n", encoding="utf-8-sig")
    args = f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{ps1}"'
    if is_admin():
        r = proc.run(["powershell"] + proc.split(args), timeout=timeout_s)
        text = logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else r.out
        return "" if r.ok else _last_error(text) or r.tail(2)

    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040            # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb, sei.lpFile, sei.lpParameters, sei.nShow = "runas", "powershell.exe", args, 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return "elevation refused or failed (UAC prompt cancelled?)"
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    if k32.WaitForSingleObject(sei.hProcess, int(timeout_s * 1000)) != 0:
        k32.CloseHandle(sei.hProcess)
        return f"timed out after {timeout_s / 60:.0f} min"
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    k32.CloseHandle(sei.hProcess)
    text = logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""
    return "" if code.value == 0 else (_last_error(text) or f"exit {code.value}")


def _last_error(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.startswith("ERROR:"):
            return line[6:].strip()
    return ""


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class WindowsServices(Provider):
    def __init__(self, cfg: Config) -> None:
        pass

    def states(self, systems: List[System]) -> Dict[str, str]:
        names = sorted({n for s in systems for n in s.services})
        svc = _service_states(names) if names else {}
        out = {}
        for s in systems:
            st = [svc.get(n, "unknown") for n in s.services]
            if any(x == "missing" for x in st):
                out[s.name] = State.MISSING
            elif all(x == "running" for x in st):
                out[s.name] = State.RUNNING
            elif all(x == "stopped" for x in st):
                out[s.name] = State.STOPPED
            elif any(x == "unknown" for x in st):
                out[s.name] = State.UNKNOWN
            else:
                out[s.name] = State.PARTIAL
        return out

    def preflight(self, system: System) -> str:
        missing = [n for n, st in _service_states(system.services).items() if st == "missing"]
        return f"service(s) not installed: {', '.join(missing)}" if missing else ""

    def start(self, system: System) -> str:
        # In order; Start-Service waits for each to report Running before the next.
        lines = [f"Start-Service -Name {_ps_quote(n)}; Write-Output 'started {n}'" for n in system.services]
        return _run_elevated_ps("\n".join(lines), timeout_s=30 * 60)

    def stop(self, system: System, timeout_s: float) -> str:
        lines = [f"Stop-Service -Name {_ps_quote(n)} -Force; Write-Output 'stopped {n}'"
                 for n in reversed(system.services)]
        return _run_elevated_ps("\n".join(lines), timeout_s=timeout_s)
