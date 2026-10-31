"""Ordered start / stop with real readiness. No UI here: the tray (or CLI) runs these off-thread.

Start, per system in list order:
  preflight -> resource ceiling -> [ensure UTC RTC] -> beforeStart hooks -> provider start
  -> wait for SSH (only if the system has ssh probes) -> afterBoot hooks
  -> readiness: every probe passes continuously for settleSeconds, within timeoutMinutes
  -> afterReady hooks.
A critical system that is not ready aborts the chain; any other is marked degraded and the chain
continues. Systems already running still go through readiness and hooks (hooks must be idempotent).

Stop, reverse list order: beforeStop hooks -> provider stop (graceful only) -> afterStop hooks.
If one system will not stop, the chain halts: stopping a dependency under a system that is still
running is worse than leaving both up.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import probes, proc
from .config import Config, Hook, System, expand
from .providers import Provider, State, providers_for

log = logging.getLogger(__name__)

# (system, check, status PASS/WARN/FAIL/INFO, detail) -- converted to health items by the caller
Finding = Tuple[str, str, str, str]


class Cancelled(Exception):
    pass


@dataclass
class OpResult:
    op: str                                   # 'start' | 'stop'
    verdict: str                              # READY | DEGRADED | FAILED | CANCELLED | STOPPED
    ready: List[str] = field(default_factory=list)
    degraded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        parts = [self.verdict]
        if self.degraded:
            parts.append("degraded: " + ", ".join(self.degraded))
        if self.failed:
            parts.append("failed: " + ", ".join(self.failed))
        return " -- ".join(parts)


class Engine:
    def __init__(self, cfg: Config, providers: Optional[Dict[str, Provider]] = None,
                 on_progress: Optional[Callable[[str], None]] = None,
                 probe_fn: Callable = probes.run_probe, ssh_ok: Callable = probes.ssh_reachable,
                 hook_fn: Optional[Callable[[Hook, System, str], proc.Result]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.providers = providers if providers is not None else providers_for(cfg)
        self.on_progress = on_progress or (lambda _m: None)
        self.probe_fn, self.ssh_ok = probe_fn, ssh_ok
        self.hook_fn = hook_fn or self._run_hook
        self.clock = clock
        self.cancel_event = threading.Event()

    # ------------------------------------------------------------------ helpers
    def _say(self, msg: str, level: int = logging.INFO) -> None:
        log.log(level, msg)
        self.on_progress(msg)

    def _wait(self, seconds: float) -> None:
        if self.cancel_event.wait(seconds):
            raise Cancelled()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def _select(self, names: Optional[List[str]]) -> List[System]:
        if names is None:
            return self.cfg.enabled_systems()
        return [s for s in self.cfg.systems if s.name in names]   # config order, not caller order

    def states(self, systems: Optional[List[System]] = None) -> Dict[str, str]:
        systems = self.cfg.systems if systems is None else systems
        out: Dict[str, str] = {}
        for typ, prov in self.providers.items():
            group = [s for s in systems if s.type == typ]
            if group:
                try:
                    out.update(prov.states(group))
                except Exception as e:  # never let a status poll kill the caller
                    log.warning("%s status failed: %s", typ, e)
                    out.update({s.name: State.UNKNOWN for s in group})
        return out

    # ------------------------------------------------------------------ hooks
    def _run_hook(self, hook: Hook, system: System, phase: str) -> proc.Result:
        env = {"SS_SYSTEM": system.name, "SS_HOST": system.host, "SS_PHASE": phase,
               "SS_VMX": expand(system.vmx, system) if system.vmx else ""}
        return proc.run(expand(hook.cmd, system), timeout=hook.timeoutMinutes * 60, env=env, log_output=True)

    def _hooks(self, system: System, phase: str, res: OpResult) -> bool:
        """Run a phase's hooks. Returns False if a required hook failed."""
        ok = True
        for hook in system.hooks_for(phase):
            self._check_cancel()
            self._say(f"{system.name}: {phase} hook: {hook.cmd}")
            r = self.hook_fn(hook, system, phase)
            if r.ok:
                res.findings.append((system.name, f"hook {phase}", "PASS", hook.cmd))
                continue
            status = "FAIL" if hook.required else "WARN"
            detail = f"exit {r.code}: {r.tail(3)}"
            res.findings.append((system.name, f"hook {phase}", status, f"{hook.cmd} -> {detail}"))
            self._say(f"{system.name}: {phase} hook failed ({detail})", logging.WARNING)
            ok = ok and not hook.required
        return ok

    # ------------------------------------------------------------------ ceiling
    def _ceiling_error(self, system: System) -> str:
        g = self.cfg.general
        if system.type != "vmware" or (g.maxGuestGB <= 0 and g.maxVcpu <= 0):
            return ""
        prov = self.providers["vmware"]
        running = prov.running()  # type: ignore[attr-defined]
        mem = cpu = 0.0
        paths = list(running) + [os.path.normcase(os.path.normpath(expand(system.vmx, system)))]
        for p in paths:
            r = prov.resources(System(name=p, vmx=p))
            mem += r.get("memGB", 0)
            cpu += r.get("vcpu", 0)
        if (g.maxGuestGB and mem > g.maxGuestGB) or (g.maxVcpu and cpu > g.maxVcpu):
            return (f"starting it would take running VMs to {mem:g} GB / {cpu:g} vCPU "
                    f"(ceiling {g.maxGuestGB or '-'} GB / {g.maxVcpu or '-'} vCPU)")
        return ""

    # ------------------------------------------------------------------ readiness
    def wait_ready(self, system: System) -> Tuple[bool, str]:
        rd = system.ready
        if not rd.probes:
            return True, "no readiness probes configured"
        deadline = self.clock() + rd.timeoutMinutes * 60
        passed_at: Optional[float] = None
        last = ""
        while True:
            results = [(p.display, self.probe_fn(p, system)) for p in rd.probes]
            summary = ", ".join(f"{name}={r.detail}" for name, r in results)
            if summary != last:
                log.info("%s: probes %s", system.name, summary)
                last = summary
            if all(r.ok for _n, r in results):
                if passed_at is None:
                    passed_at = self.clock()
                    if rd.settleSeconds:
                        self._say(f"{system.name}: probes green, holding {rd.settleSeconds}s")
                if self.clock() - passed_at >= rd.settleSeconds:
                    return True, summary
            else:
                if passed_at is not None:
                    self._say(f"{system.name}: a probe dropped during settle ({summary}); restarting settle",
                              logging.WARNING)
                passed_at = None
            if self.clock() >= deadline:
                return False, summary
            self._wait(rd.intervalSeconds)

    def _wait_ssh(self, system: System) -> bool:
        deadline = self.clock() + system.ready.timeoutMinutes * 60
        while not self.ssh_ok(system):
            if self.clock() >= deadline:
                return False
            self._wait(5)
        return True

    # ------------------------------------------------------------------ start
    def start(self, names: Optional[List[str]] = None) -> OpResult:
        self.cancel_event.clear()
        res = OpResult("start", "READY")
        t0 = self.clock()
        try:
            for system in self._select(names):
                self._check_cancel()
                if not self._start_one(system, res):
                    res.verdict = "FAILED"
                    self._say(f"{system.name} is critical and not ready -- stopping the start chain here",
                              logging.ERROR)
                    break
            self.info_probes(self._select(names), res)
        except Cancelled:
            res.verdict = "CANCELLED"
            self._say("start cancelled (nothing was powered off)", logging.WARNING)
        if res.verdict == "READY" and (res.degraded or res.failed):
            res.verdict = "DEGRADED"
        res.seconds = self.clock() - t0
        return res

    def _start_one(self, system: System, res: OpResult) -> bool:
        """Returns False only when the chain must abort (critical system not ready)."""
        prov = self.providers[system.type]
        state = self.states([system]).get(system.name, State.UNKNOWN)
        t0 = self.clock()

        def fail(msg: str) -> bool:
            self._say(f"{system.name}: {msg}", logging.ERROR)
            res.findings.append((system.name, "start", "FAIL", msg))
            (res.failed if system.critical else res.degraded).append(system.name)
            return not system.critical

        if state == State.RUNNING:
            self._say(f"{system.name}: already running -- checking readiness")
        else:
            err = prov.preflight(system) or self._ceiling_error(system)
            if err:
                return fail(err)
            if state == State.SUSPENDED and system.onSuspended == "coldBoot":
                err = self._cold_boot_from_suspend(system, prov)
                if err:
                    return fail(err)
                state = State.STOPPED
            if system.type == "vmware" and system.ensureUtcRtc and state == State.STOPPED:
                if prov.ensure_utc_rtc(system):  # type: ignore[attr-defined]
                    self._say(f"{system.name}: set rtc.diffFromUTC = 0 (UTC hardware clock)")
            if not self._hooks(system, "beforeStart", res):
                return fail("required beforeStart hook failed")
            self._say(f"{system.name}: starting" + (" (resuming)" if state == State.SUSPENDED else ""))
            err = prov.start(system)
            if err:
                return fail(err)

        if system.uses_ssh:
            self._say(f"{system.name}: waiting for SSH")
            if not self._wait_ssh(system):
                return fail(f"no SSH after {system.ready.timeoutMinutes} min")
        if not self._hooks(system, "afterBoot", res):
            return fail("required afterBoot hook failed")

        self._say(f"{system.name}: waiting for readiness")
        ok, detail = self.wait_ready(system)
        if not ok:
            return fail(f"not ready after {system.ready.timeoutMinutes} min: {detail}")
        secs = self.clock() - t0
        self._say(f"{system.name}: ready after {secs:.0f}s")
        res.findings.append((system.name, "ready", "PASS", f"{detail} (after {secs:.0f}s)"))

        if not self._hooks(system, "afterReady", res):
            res.degraded.append(system.name)
            return True
        res.ready.append(system.name)
        return True

    def _cold_boot_from_suspend(self, system: System, prov: Provider) -> str:
        """A suspended guest resumes with every process frozen across the gap and the clock jumped;
        long-running server stacks often misbehave afterwards. Resume, shut down cleanly, and let the
        normal start path boot it fresh. Returns '' or an error."""
        self._say(f"{system.name}: suspended -- resuming to shut it down cleanly, then cold boot (onSuspended=coldBoot)")
        err = prov.start(system)
        if err:
            return f"resume failed: {err}"
        self._wait(15)                      # let VMware Tools in the guest answer the shutdown request
        err = prov.stop(system, system.stopTimeoutMinutes * 60)
        if err:
            return f"clean shutdown after resume failed: {err}"
        self._say(f"{system.name}: shut down cleanly after resume")
        return ""

    def info_probes(self, systems: List[System], res: OpResult) -> None:
        states = self.states(systems)
        for s in systems:
            if states.get(s.name) != State.RUNNING:
                continue
            for p in s.infoProbes:
                r = self.probe_fn(p, s)
                if not r.ok:
                    res.findings.append((s.name, p.display, "INFO", r.detail))
                    self._say(f"{s.name}: {p.display} ({r.detail})")

    # ------------------------------------------------------------------ stop
    def stop(self, names: Optional[List[str]] = None) -> OpResult:
        self.cancel_event.clear()
        res = OpResult("stop", "STOPPED")
        t0 = self.clock()
        try:
            systems = list(reversed(self._select(names)))
            states = self.states(systems)
            for system in systems:
                self._check_cancel()
                if states.get(system.name) in (State.STOPPED, State.MISSING):
                    self._say(f"{system.name}: already stopped")
                    continue
                if states.get(system.name) == State.SUSPENDED:
                    self._say(f"{system.name}: suspended -- leaving it (resume with Start)")
                    continue
                self._hooks(system, "beforeStop", res)
                self._say(f"{system.name}: stopping")
                t1 = self.clock()
                err = self.providers[system.type].stop(system, system.stopTimeoutMinutes * 60)
                if err:
                    self._say(f"{system.name}: {err}", logging.ERROR)
                    res.findings.append((system.name, "stop", "FAIL", err))
                    res.failed.append(system.name)
                    res.verdict = "FAILED"
                    self._say("halting the stop chain so nothing it depends on is stopped underneath it",
                              logging.ERROR)
                    break
                self._say(f"{system.name}: stopped after {self.clock() - t1:.0f}s")
                self._hooks(system, "afterStop", res)
        except Cancelled:
            res.verdict = "CANCELLED"
            self._say("stop cancelled", logging.WARNING)
        res.seconds = self.clock() - t0
        return res

    def cancel(self) -> None:
        self.cancel_event.set()
