"""Engine behaviour with a fake provider, fake probes and a fake clock (no VMs, no sleeping)."""
from __future__ import annotations

from typing import Dict, List

import pytest

from starterstopper import proc
from starterstopper.config import from_dict
from starterstopper.engine import Engine
from starterstopper.probes import ProbeResult
from starterstopper.providers.base import Provider, State


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeProvider(Provider):
    def __init__(self, running=(), stuck=(), mem=None) -> None:
        self.running_set = set(running)
        self.stuck = set(stuck)
        self.calls: List[str] = []
        self.mem = mem or {}

    def states(self, systems):
        return {s.name: State.RUNNING if s.name in self.running_set else State.STOPPED for s in systems}

    def start(self, system):
        self.calls.append(f"start {system.name}")
        self.running_set.add(system.name)
        return ""

    def stop(self, system, timeout_s):
        self.calls.append(f"stop {system.name}")
        if system.name in self.stuck:
            return "still running"
        self.running_set.discard(system.name)
        return ""

    # ceiling support
    def running(self):
        return {f"c:\\vms\\{n}.vmx" for n in self.running_set}

    def resources(self, system):
        name = system.vmx.rsplit("\\", 1)[-1].replace(".vmx", "")
        return {"memGB": self.mem.get(name, 8), "vcpu": 4}

    def ensure_utc_rtc(self, system):
        return False


def cfg(systems: List[dict], **general):
    for s in systems:
        s.setdefault("vmx", f"C:\\VMs\\{s['name']}.vmx")
        s.setdefault("ready", {"settleSeconds": 30, "timeoutMinutes": 5, "intervalSeconds": 10,
                               "probes": [{"http": "https://x/{name}"}]})
    return from_dict({"general": general, "systems": systems})


def make(config, provider, probe_seq: Dict[str, List[bool]] = None, hook_codes=None):
    """probe_seq: per system, successive probe results (last value repeats)."""
    clock = FakeClock()
    probe_seq = probe_seq or {}
    counters: Dict[str, int] = {}
    hooks_run: List[str] = []

    def probe_fn(p, s):
        seq = probe_seq.get(s.name, [True])
        i = counters.get(s.name, 0)
        counters[s.name] = i + 1
        ok = seq[min(i, len(seq) - 1)]
        return ProbeResult(ok, "200" if ok else "503")

    def hook_fn(h, s, phase):
        hooks_run.append(f"{s.name}:{phase}")
        code = (hook_codes or {}).get(f"{s.name}:{phase}", 0)
        return proc.Result(code, "", "boom" if code else "")

    e = Engine(config, providers={"vmware": provider}, probe_fn=probe_fn,
               ssh_ok=lambda s: True, hook_fn=hook_fn, clock=clock)
    # Replace the real wait with a clock advance.
    e._wait = lambda secs: setattr(clock, "t", clock.t + secs)
    e.hooks_run = hooks_run
    return e


def test_start_order_and_ready():
    c = cfg([{"name": "base", "critical": True}, {"name": "knowledge"}, {"name": "velocity"}])
    p = FakeProvider()
    r = make(c, p).start()
    assert p.calls == ["start base", "start knowledge", "start velocity"]
    assert r.verdict == "READY" and r.ready == ["base", "knowledge", "velocity"]


def test_settle_resets_when_probe_flaps():
    # green, green, red (resets settle), then green for good. interval 10, settle 30.
    c = cfg([{"name": "base"}])
    e = make(c, FakeProvider(), probe_seq={"base": [True, True, False, True]})
    r = e.start()
    assert r.verdict == "READY"
    # flap at t=20 -> new passed_at at t=30 -> ready at t=60
    assert e.clock() == 60


def test_critical_not_ready_aborts_chain():
    c = cfg([{"name": "base", "critical": True}, {"name": "velocity"}])
    p = FakeProvider()
    r = make(c, p, probe_seq={"base": [False]}).start()
    assert r.verdict == "FAILED" and r.failed == ["base"]
    assert "start velocity" not in p.calls


def test_noncritical_not_ready_degrades_and_continues():
    c = cfg([{"name": "base", "critical": True}, {"name": "velocity"}, {"name": "knowledge"}])
    p = FakeProvider()
    r = make(c, p, probe_seq={"velocity": [False]}).start()
    assert r.verdict == "DEGRADED" and r.degraded == ["velocity"]
    assert r.ready == ["base", "knowledge"]


def test_ceiling_refuses_start():
    c = cfg([{"name": "base"}, {"name": "big"}], maxGuestGB=20)
    p = FakeProvider(mem={"base": 16, "big": 16})
    r = make(c, p).start()
    assert "start big" not in p.calls
    assert r.verdict == "DEGRADED" and "big" in r.degraded


def test_already_running_still_checked_and_hooks_run():
    c = cfg([{"name": "velocity", "hooks": {"afterReady": ["repair.ps1"]}}])
    p = FakeProvider(running={"velocity"})
    e = make(c, p)
    r = e.start()
    assert p.calls == [] and r.verdict == "READY"
    assert e.hooks_run == ["velocity:afterBoot", "velocity:afterReady"][1:]  # no afterBoot hooks configured


def test_required_hook_failure_degrades():
    c = cfg([{"name": "velocity", "hooks": {"afterReady": [{"cmd": "repair.ps1", "required": True}]}}])
    r = make(c, FakeProvider(), hook_codes={"velocity:afterReady": 1}).start()
    assert r.verdict == "DEGRADED" and r.degraded == ["velocity"]


def test_optional_hook_failure_warns_only():
    c = cfg([{"name": "velocity", "hooks": {"afterReady": ["repair.ps1"]}}])
    r = make(c, FakeProvider(), hook_codes={"velocity:afterReady": 1}).start()
    assert r.verdict == "READY"
    assert any(f[2] == "WARN" for f in r.findings)


def test_stop_reverse_order_halts_on_stuck():
    c = cfg([{"name": "base"}, {"name": "knowledge"}, {"name": "velocity"}])
    p = FakeProvider(running={"base", "knowledge", "velocity"}, stuck={"knowledge"})
    r = make(c, p).stop()
    assert p.calls == ["stop velocity", "stop knowledge"]      # base never stopped
    assert r.verdict == "FAILED" and r.failed == ["knowledge"]


def test_disabled_systems_skipped_by_start_all_but_startable_by_name():
    c = cfg([{"name": "base"}, {"name": "extra", "enabled": False}])
    p = FakeProvider()
    e = make(c, p)
    e.start()
    assert p.calls == ["start base"]
    e.start(["extra"])
    assert p.calls[-1] == "start extra"


def test_cancel():
    c = cfg([{"name": "base"}])
    e = make(c, FakeProvider(), probe_seq={"base": [False]})
    e.cancel_event.set()
    e._wait = lambda s: (_ for _ in ()).throw(__import__("starterstopper.engine").engine.Cancelled())
    r = e.start()
    assert r.verdict == "CANCELLED"


@pytest.mark.parametrize("bad", [
    {"systems": [{"name": "a"}]},                                             # vmware without vmx
    {"systems": [{"name": "a", "type": "windows-service"}]},                  # no services
    {"systems": [{"name": "a", "vmx": "x", "ready": {"probes": [{"ssh": "true"}]}}]},  # ssh without host
    {"systems": [{"name": "a", "vmx": "x"}, {"name": "a", "vmx": "y"}]},      # duplicate
    {"systems": [{"name": "a", "vmx": "x", "hooks": {"whenever": ["x"]}}]},   # bad phase
    {"health": {"checks": [{"name": "h", "cmd": "x", "format": "xml"}]}},    # bad format
])
def test_config_validation(bad):
    from starterstopper.config import ConfigError
    with pytest.raises(ConfigError):
        from_dict(bad)


class SuspendingProvider(FakeProvider):
    def __init__(self, suspended=()):
        super().__init__()
        self.suspended = set(suspended)

    def states(self, systems):
        out = super().states(systems)
        for s in systems:
            if s.name in self.suspended and s.name not in self.running_set:
                out[s.name] = State.SUSPENDED
        return out

    def start(self, system):
        self.suspended.discard(system.name)
        return super().start(system)


def test_suspended_cold_boot_resumes_stops_then_starts():
    c = cfg([{"name": "base", "onSuspended": "coldBoot"}])
    p = SuspendingProvider(suspended={"base"})
    r = make(c, p).start()
    assert p.calls == ["start base", "stop base", "start base"]
    assert r.verdict == "READY"


def test_suspended_default_just_resumes():
    c = cfg([{"name": "base"}])
    p = SuspendingProvider(suspended={"base"})
    make(c, p).start()
    assert p.calls == ["start base"]
