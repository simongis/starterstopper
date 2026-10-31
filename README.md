# StarterStopper

A small Windows system-tray app that starts, stops and health-checks a home lab or sandbox: VMware
Workstation VMs and/or Windows services, in dependency order, with real readiness checks instead of fixed
sleeps. Built for ArcGIS Enterprise labs, but nothing in it is ArcGIS-specific except the bundled health
probe.

- **No console window.** Runs from a Startup-folder shortcut via `pythonw`, unelevated.
- **Ordered start / stop.** List order is start order; stop is the reverse. Stop is graceful only (no hard
  power-off), and halts the chain if a system won't stop.
- **Real readiness.** Each system has probes (HTTP from this PC, or a command over SSH in the guest). All must
  stay green for a settle window (default 30 s) before the next system starts. A one-off 200 doesn't count.
- **Hooks.** Run your own scripts at `beforeStart`, `afterBoot`, `afterReady`, `beforeStop`, `afterStop`
  (for example, a self-repair step for a component that needs it).
- **Health check without an LLM.** Runs any check command that prints JSON `{check,status,detail}` rows
  (or just uses its exit code), applies a table of known false alarms and fix hints, and writes an HTML
  report. Includes `health/Test-ArcGISEnterprise.ps1`, a profile-driven ArcGIS Enterprise REST probe.
- **Guard rails.** Optional RAM/vCPU ceiling across running VMs; warns if the Windows hypervisor is loaded
  (VMware Workstation then runs on a slower path); reports pending guest reboots.
- **Light.** About 40 MB resident; one batched status query a minute when idle. Preferences opens as a
  separate process.

## Tray icon

| Colour | Meaning |
|---|---|
| Green | All systems in *Start all* are running, and the last start/health check was clean |
| Amber | Running but degraded (see the health report), or only some systems running |
| Red | All stopped |
| Yellow | Start / stop / health check in progress (tooltip shows the current step) |
| Grey | Unknown, or nothing configured |
| Yellow `!` badge | Config error, or the Windows hypervisor is loaded |

Menu: Start all, Stop all, Cancel, Systems (start/stop each one, show it in Workstation), Run health
check, Open last health report, your links, Preferences, Open log, Quit.

## Install

Requirements: Windows 10/11, Python 3.9+ (python.org build), PowerShell 7 (`pwsh`) for the bundled health
probe, VMware Workstation for VM systems, and the Windows OpenSSH client if you use SSH probes.

```powershell
git clone <this repository URL>
cd starterstopper
.\install.ps1 -InstallDeps -Launch      # Startup shortcut + start now. -DesktopShortcut adds one there too.
```

Then right-click the tray icon > **Preferences...** > **Load example**, edit it, and Save. Uninstall with
`.\uninstall.ps1` (add `-RemoveData` to delete config, logs and reports).

## Configuration

`%APPDATA%\StarterStopper\config.json`, edited in Preferences or by hand (the app reloads it when it changes).
It is outside the repo on purpose. Start from [`config.example.json`](config.example.json).

| Section | Key fields |
|---|---|
| `general` | `launchAtLogin`, `startAllAtLogin` (only within 15 min of boot), `idlePollSeconds`, `maxGuestGB` / `maxVcpu` (0 = no ceiling), `vmrunPath` (blank = auto-detect), `checkHypervisor`, `notifications` |
| `systems[]` | `name`, `type` (`vmware` or `windows-service`), `enabled` (in *Start all*), `critical` (not ready = abort the chain), `host`, `vmx` or `services`, `ensureUtcRtc`, `stopTimeoutMinutes`, `ssh` {`user`, `keyPath`, `port`}, `ready` {`settleSeconds`, `timeoutMinutes`, `intervalSeconds`, `probes`}, `infoProbes`, `hooks` |
| `health` | `runAfterStart`, `checks[]` {`name`, `cmd`, `format` `json`/`exitcode`, `timeoutMinutes`}, `hintsFile` |
| `links[]` | `label`, `url` (shown under *Open*) |

Probes: `{ "label": "Portal", "http": "https://{host}:7443/arcgis/portaladmin/healthCheck" }` (passes on
`expect`, default `[200]`; TLS not verified unless `verifyTls`) or
`{ "label": "no OS upgrade running", "ssh": "! systemctl is-active -q apt-daily-upgrade.service" }` (passes on
exit 0). SSH uses key auth only (BatchMode), never a password.

Placeholders in commands, URLs and paths: `{host}`, `{name}` (per system), `{app}` (this folder),
`{config_dir}` (the config folder); `%ENV%` and `~` are expanded. Hooks also get `SS_SYSTEM`, `SS_HOST`,
`SS_PHASE` and `SS_VMX` environment variables, and must be idempotent: they run on every start, even when
the system was already running.

**Secrets never go in the config.** The health probe takes credentials as environment-variable names, or
an `.env` file path, in its own profile. See [health/README.md](health/README.md).

## Command line

```powershell
python StarterStopper.pyw --cli status
python StarterStopper.pyw --cli start [names...]     # exit 0 = READY
python StarterStopper.pyw --cli stop  [names...]
python StarterStopper.pyw --cli health
```

Logs: `%LOCALAPPDATA%\StarterStopper\logs\starterstopper.log`. Reports:
`%LOCALAPPDATA%\StarterStopper\reports\` (`last.html` is the newest).

## Windows services

`windows-service` systems (for example ArcGIS Enterprise installed on a laptop) read their status without
admin rights. Start/stop asks for elevation once per action (UAC), runs hidden, and starts services in the
listed order (stops in reverse).

## Development

```powershell
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
git config core.hooksPath .githooks      # enable the leak check before every commit
```

Leak checks guard the public GitHub copy:

- **pre-push to github.com:** [gitleaks](https://github.com/gitleaks/gitleaks) scans every commit being pushed
  (`winget install gitleaks`; the push is blocked if it is missing). `tools/check_sanitised.py` then checks each
  changed file for private IPs, likely secrets, and terms from your own gitignored `.sanitise-deny.txt` (see
  `.sanitise-deny.example.txt`). Pushes to other remotes, such as a private mirror, are not checked.
- **GitHub:** `.github/workflows/gitleaks.yml` scans every push and PR. Also keep secret-scanning push protection
  on in your GitHub settings.

The check covers the whole history being pushed, so keep private details out of commits on any branch you
intend to publish.

Use documentation IPs (`192.0.2.x`) and `example.com` in anything committed.
