"""Configuration: schema, defaults, validation, load/save.

The config file lives outside the repo (``%APPDATA%\\StarterStopper\\config.json``) so a
user's hosts, paths and IPs never end up in source control. Secrets are never stored here:
health checks reference environment-variable names or an ``.env`` file path instead.

Placeholders expanded at run time in commands, URLs and paths:
    {host}        the system's ``host`` value
    {name}        the system's ``name``
    {app}         the StarterStopper install folder (repo root)
    {config_dir}  the folder holding config.json
Environment variables (``%VAR%``) and ``~`` are expanded in paths and commands.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
APP_NAME = "StarterStopper"

APP_DIR = Path(__file__).resolve().parents[2]          # repo root (src/starterstopper/config.py)
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"
REPORT_DIR = DATA_DIR / "reports"

SYSTEM_TYPES = ("vmware", "windows-service")
HOOK_PHASES = ("beforeStart", "afterBoot", "afterReady", "beforeStop", "afterStop")
HEALTH_FORMATS = ("json", "exitcode")

DEFAULT_VMRUN = r"C:\Program Files\VMware\VMware Workstation\vmrun.exe"
DEFAULT_VMRUN_X86 = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"


class ConfigError(ValueError):
    """Raised when a config file is missing required values or has invalid ones."""


# ---------------------------------------------------------------------------- schema


@dataclass
class Probe:
    """One readiness / info probe. Exactly one of ``http`` or ``ssh`` is set."""
    http: Optional[str] = None             # URL; placeholders allowed
    ssh: Optional[str] = None              # shell command run in the guest; pass = exit 0
    label: Optional[str] = None            # shown in logs/menus; defaults to the URL/command
    expect: List[int] = field(default_factory=lambda: [200])   # http status codes that pass
    verifyTls: bool = False                # labs usually have self-signed / internal-CA certs
    timeoutSeconds: int = 5

    @property
    def kind(self) -> str:
        return "http" if self.http else "ssh"

    @property
    def display(self) -> str:
        return self.label or self.http or self.ssh or "?"


@dataclass
class SshSettings:
    user: str = ""
    keyPath: str = ""                      # private key; empty = ssh default identity / agent
    port: int = 22


@dataclass
class Readiness:
    settleSeconds: int = 30                # all probes must stay green this long
    timeoutMinutes: int = 15
    intervalSeconds: int = 10
    probes: List[Probe] = field(default_factory=list)


@dataclass
class Hook:
    cmd: str = ""
    required: bool = False                 # True: non-zero exit fails the system; False: warn
    timeoutMinutes: int = 15


@dataclass
class System:
    name: str = ""
    type: str = "vmware"
    enabled: bool = True                   # included in Start all / Stop all
    critical: bool = False                 # not ready -> abort the rest of the start chain
    host: str = ""                         # IP / DNS name used by probes, ssh and {host}
    # vmware
    vmx: str = ""
    ensureUtcRtc: bool = False             # set rtc.diffFromUTC = "0" in the .vmx while off (Linux guests)
    stopTimeoutMinutes: int = 10
    # windows-service
    services: List[str] = field(default_factory=list)   # internal service names, start order
    # common
    ssh: SshSettings = field(default_factory=SshSettings)
    ready: Readiness = field(default_factory=Readiness)
    infoProbes: List[Probe] = field(default_factory=list)  # reported only (e.g. reboot required)
    hooks: Dict[str, List[Hook]] = field(default_factory=dict)

    def hooks_for(self, phase: str) -> List[Hook]:
        return self.hooks.get(phase, [])

    @property
    def uses_ssh(self) -> bool:
        probes = self.ready.probes + self.infoProbes
        return any(p.ssh for p in probes)


@dataclass
class HealthCheck:
    name: str = ""
    cmd: str = ""
    format: str = "exitcode"               # json: stdout is [{check,status,detail}]; exitcode: 0 = pass
    timeoutMinutes: int = 10
    enabled: bool = True


@dataclass
class Health:
    runAfterStart: bool = True
    checks: List[HealthCheck] = field(default_factory=list)
    hintsFile: str = ""                    # optional extra hints JSON merged over the built-in ones


@dataclass
class Link:
    label: str = ""
    url: str = ""


@dataclass
class General:
    launchAtLogin: bool = True
    startAllAtLogin: bool = False
    idlePollSeconds: int = 60
    busyPollSeconds: int = 5
    maxGuestGB: int = 0                    # 0 = no ceiling
    maxVcpu: int = 0                       # 0 = no ceiling
    vmrunPath: str = ""                    # empty = auto-detect
    checkHypervisor: bool = True           # warn if Windows hypervisor is loaded (slows VMware)
    notifications: bool = True


@dataclass
class Config:
    version: int = SCHEMA_VERSION
    general: General = field(default_factory=General)
    systems: List[System] = field(default_factory=list)
    health: Health = field(default_factory=Health)
    links: List[Link] = field(default_factory=list)

    # -- convenience ----------------------------------------------------------------
    def enabled_systems(self) -> List[System]:
        """Systems included in Start all / Stop all, in start order (list order)."""
        return [s for s in self.systems if s.enabled]

    def system(self, name: str) -> System:
        for s in self.systems:
            if s.name == name:
                return s
        raise KeyError(name)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------- parse


def _probe(d: Any, where: str) -> Probe:
    if isinstance(d, str):                 # shorthand: a bare URL
        d = {"http": d}
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: probe must be an object or URL string")
    p = Probe(
        http=d.get("http") or None,
        ssh=d.get("ssh") or None,
        label=d.get("label") or None,
        expect=[int(x) for x in d.get("expect", [200])],
        verifyTls=bool(d.get("verifyTls", False)),
        timeoutSeconds=int(d.get("timeoutSeconds", 5)),
    )
    if bool(p.http) == bool(p.ssh):
        raise ConfigError(f"{where}: probe needs exactly one of 'http' or 'ssh'")
    return p


def _hook(d: Any, where: str) -> Hook:
    if isinstance(d, str):
        d = {"cmd": d}
    if not isinstance(d, dict) or not d.get("cmd"):
        raise ConfigError(f"{where}: hook needs 'cmd'")
    return Hook(cmd=d["cmd"], required=bool(d.get("required", False)),
                timeoutMinutes=int(d.get("timeoutMinutes", 15)))


def _system(d: Dict[str, Any], i: int) -> System:
    where = f"systems[{i}]"
    name = (d.get("name") or "").strip()
    if not name:
        raise ConfigError(f"{where}: 'name' is required")
    where = f"system '{name}'"
    typ = d.get("type", "vmware")
    if typ not in SYSTEM_TYPES:
        raise ConfigError(f"{where}: type must be one of {', '.join(SYSTEM_TYPES)}")

    ssh = d.get("ssh") or {}
    ready = d.get("ready") or {}
    hooks_in = d.get("hooks") or {}
    hooks: Dict[str, List[Hook]] = {}
    for phase, items in hooks_in.items():
        if phase not in HOOK_PHASES:
            raise ConfigError(f"{where}: unknown hook phase '{phase}' (use {', '.join(HOOK_PHASES)})")
        if isinstance(items, (str, dict)):
            items = [items]
        hooks[phase] = [_hook(h, f"{where} hook {phase}") for h in items]

    s = System(
        name=name,
        type=typ,
        enabled=bool(d.get("enabled", True)),
        critical=bool(d.get("critical", False)),
        host=d.get("host", "") or "",
        vmx=d.get("vmx", "") or "",
        ensureUtcRtc=bool(d.get("ensureUtcRtc", False)),
        stopTimeoutMinutes=int(d.get("stopTimeoutMinutes", 10)),
        services=list(d.get("services") or []),
        ssh=SshSettings(user=ssh.get("user", "") or "", keyPath=ssh.get("keyPath", "") or "",
                        port=int(ssh.get("port", 22))),
        ready=Readiness(
            settleSeconds=int(ready.get("settleSeconds", 30)),
            timeoutMinutes=int(ready.get("timeoutMinutes", 15)),
            intervalSeconds=int(ready.get("intervalSeconds", 10)),
            probes=[_probe(p, f"{where} ready.probes[{j}]") for j, p in enumerate(ready.get("probes") or [])],
        ),
        infoProbes=[_probe(p, f"{where} infoProbes[{j}]") for j, p in enumerate(d.get("infoProbes") or [])],
        hooks=hooks,
    )
    if typ == "vmware" and not s.vmx:
        raise ConfigError(f"{where}: VMware systems need 'vmx' (path to the .vmx file)")
    if typ == "windows-service" and not s.services:
        raise ConfigError(f"{where}: Windows-service systems need 'services' (service names)")
    if s.uses_ssh and not s.host:
        raise ConfigError(f"{where}: ssh probes need 'host'")
    if any(p.http and "{host}" in p.http for p in s.ready.probes + s.infoProbes) and not s.host:
        raise ConfigError(f"{where}: a probe uses {{host}} but 'host' is empty")
    if s.ready.settleSeconds < 0 or s.ready.timeoutMinutes <= 0 or s.ready.intervalSeconds <= 0:
        raise ConfigError(f"{where}: readiness timings must be positive")
    return s


def from_dict(d: Dict[str, Any]) -> Config:
    """Parse and validate. Any type error in the input is reported as ConfigError."""
    try:
        return _from_dict(d)
    except ConfigError:
        raise
    except (TypeError, ValueError, AttributeError) as e:
        raise ConfigError(f"invalid value in config: {e}") from e


def _from_dict(d: Dict[str, Any]) -> Config:
    if not isinstance(d, dict):
        raise ConfigError("config root must be a JSON object")
    version = int(d.get("version", SCHEMA_VERSION))
    if version > SCHEMA_VERSION:
        raise ConfigError(f"config version {version} is newer than this app understands ({SCHEMA_VERSION})")

    g = d.get("general") or {}
    general = General()
    for k in General.__dataclass_fields__:
        if k in g:
            default = getattr(general, k)
            v = g[k]
            if isinstance(default, bool):
                if not isinstance(v, bool):
                    raise ConfigError(f"general.{k} must be true or false")
            elif isinstance(default, int):
                v = int(v)
                if v < 0:
                    raise ConfigError(f"general.{k} must be 0 or more")
            else:
                v = str(v or "")
            setattr(general, k, v)
    if general.idlePollSeconds < 5 or general.busyPollSeconds < 1:
        raise ConfigError("general: poll intervals too small (idle >= 5 s, busy >= 1 s)")

    systems = [_system(s, i) for i, s in enumerate(d.get("systems") or [])]
    names = [s.name for s in systems]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate system names: {', '.join(sorted(dupes))}")

    h = d.get("health") or {}
    checks = []
    for i, c in enumerate(h.get("checks") or []):
        hc = HealthCheck(name=c.get("name", "") or f"check {i + 1}", cmd=c.get("cmd", "") or "",
                         format=c.get("format", "exitcode"), timeoutMinutes=int(c.get("timeoutMinutes", 10)),
                         enabled=bool(c.get("enabled", True)))
        if not hc.cmd:
            raise ConfigError(f"health check '{hc.name}': 'cmd' is required")
        if hc.format not in HEALTH_FORMATS:
            raise ConfigError(f"health check '{hc.name}': format must be one of {', '.join(HEALTH_FORMATS)}")
        checks.append(hc)
    health = Health(runAfterStart=bool(h.get("runAfterStart", True)), checks=checks,
                    hintsFile=h.get("hintsFile", "") or "")

    links = [Link(label=l.get("label", ""), url=l.get("url", "")) for l in d.get("links") or [] if l.get("url")]
    return Config(version=SCHEMA_VERSION, general=general, systems=systems, health=health, links=links)


# ---------------------------------------------------------------------------- io


def load(path: Path = CONFIG_PATH) -> Config:
    """Load and validate. A missing file yields an empty (valid) config."""
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON at line {e.lineno} col {e.colno}: {e.msg}") from e
    return from_dict(raw)


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    """Validate (round-trip) then write atomically, keeping one .bak of the previous file."""
    data = cfg.to_dict()
    from_dict(data)                         # raises ConfigError if invalid
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_prune(data), indent=2) + "\n", encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    os.replace(tmp, path)


def _prune(d: Any) -> Any:
    """Drop None values and empty hook maps so saved files stay readable."""
    if isinstance(d, dict):
        return {k: _prune(v) for k, v in d.items() if v is not None and not (k == "hooks" and not v)}
    if isinstance(d, list):
        return [_prune(x) for x in d]
    return d


# ---------------------------------------------------------------------------- helpers


def expand(text: str, system: Optional[System] = None) -> str:
    """Expand {host}/{name}/{app}/{config_dir}, %ENV% and a leading ~."""
    if not text:
        return text
    out = (text.replace("{app}", str(APP_DIR))
               .replace("{config_dir}", str(CONFIG_DIR)))
    if system is not None:
        out = out.replace("{host}", system.host).replace("{name}", system.name)
    out = os.path.expandvars(out)
    if out.startswith("~"):
        out = os.path.expanduser(out)
    return out


def vmrun_path(cfg: Config) -> str:
    """Configured vmrun.exe, else the first standard install location that exists, else 'vmrun'."""
    if cfg.general.vmrunPath:
        return expand(cfg.general.vmrunPath)
    for p in (DEFAULT_VMRUN, DEFAULT_VMRUN_X86):
        if Path(p).exists():
            return p
    return shutil.which("vmrun") or "vmrun"
