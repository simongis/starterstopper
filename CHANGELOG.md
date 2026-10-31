# Changelog

All notable changes to StarterStopper are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-11-01

A rewrite. StarterStopper now manages a lab of VMs (and/or Windows services) instead of ArcGIS services installed
on the host, and works for any environment through its own configuration.

### Added
- Config-driven engine: ordered start, reverse-order graceful stop, critical systems that abort the chain, and a
  RAM/vCPU ceiling across running VMs.
- Providers for VMware Workstation (via `vmrun`, no admin rights) and Windows services (status unelevated,
  start/stop through one UAC prompt per action).
- Readiness probes over HTTP(S) from the host or SSH in the guest. All must hold green for a settle window.
- Hooks at `beforeStart`, `afterBoot`, `afterReady`, `beforeStop` and `afterStop`.
- Health checks without an LLM: pluggable `json` / `exitcode` checks, a known-false-alarm hint table, and HTML
  reports. Bundles `health/Test-ArcGISEnterprise.ps1`, a profile-driven ArcGIS Enterprise REST probe.
- Suspended-VM handling: `onSuspended: coldBoot` resumes, shuts down cleanly and boots fresh. The tray warns when
  it finds VMs suspended by host sleep.
- Tray: icon states, per-system menu, Restart all, Cancel, links, last health result, notifications, an About
  item and a Windows-hypervisor warning.
- Preferences window (Systems, Health, General, Links) with probe testing, import and export.
- Command line: `--cli start|stop|health|status`, `--version`.
- `install.ps1` / `uninstall.ps1`: Startup-folder shortcut with `pythonw`, so no console window and no admin.
- User guide with screenshots, MIT licence, contributing guide.
- Release control: CI tests, gitleaks on every push, a tag-driven GitHub Release workflow, and a pre-push leak
  check (gitleaks plus a private deny-list) for the public repository.

### Changed
- Configuration lives in `%APPDATA%\StarterStopper\config.json`, never in the repository. Logs and reports live in
  `%LOCALAPPDATA%\StarterStopper\`.
- Idle footprint is about 40 MB, with one batched status query a minute.

### Removed
- The hard-coded list of ArcGIS Enterprise and Velocity Windows services and fixed start-up sleeps. The same
  setup can be described as a `windows-service` system with readiness probes.
- `launch.bat` and the requirement to run as administrator.

## [0.1.0] - 2026-04-11

First version: a Windows tray app that started and stopped ArcGIS Enterprise and ArcGIS Velocity Enterprise
Windows services on the local machine, in a fixed order with fixed waits. Required administrator rights.

[Unreleased]: https://github.com/simongis/starterstopper/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/simongis/starterstopper/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/simongis/starterstopper/releases/tag/v0.1.0
