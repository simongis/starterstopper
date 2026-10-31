# Contributing to StarterStopper

Thanks for your interest. StarterStopper is a small projectware tool for sandboxes and home labs, and
collaboration is welcome: bug reports, ideas, new providers, health probes and hint rules.

## Before you start

- **Open an issue first** for anything bigger than a small fix, so we can agree on the approach.
- Wanted most: providers for **Proxmox VE**, **Azure** and **AWS** (and Hyper-V), more health probes, and more
  hint rules for known false alarms.

## Development setup

```powershell
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
git config core.hooksPath .githooks      # enable the public-push leak check
python StarterStopper.pyw --cli status   # run against your own config
```

## Where things live

| Path | What |
|---|---|
| `src/starterstopper/engine.py` | Ordered start/stop, readiness with settle window, hooks, cancellation |
| `src/starterstopper/providers/` | One class per system type: `states()` (batched), `start()`, `stop()` |
| `src/starterstopper/probes.py` | HTTP and SSH probes |
| `src/starterstopper/health.py`, `report.py`, `hints.json` | Health checks, HTML report, hint rules |
| `src/starterstopper/tray.py` | The resident tray app (keep it light: no tkinter, no heavy imports) |
| `src/starterstopper/prefs.py` | Preferences window (separate process) |
| `health/` | Bundled ArcGIS Enterprise REST probe |
| `tests/` | pytest suite; the engine is tested with fake providers and a fake clock |

### Adding a provider

Subclass `providers.base.Provider`:

- `states(systems)` should answer for many systems with as few calls as possible. The tray polls it every minute.
- `start(system)` returns `""` on success or an error message. Readiness is the engine's job, not the provider's.
- `stop(system, timeout_s)` must be **graceful only** and confirm the system has stopped. Never fall back to a hard power-off.

Register it in `providers/__init__.py`, add the type to `SYSTEM_TYPES` in `config.py` with its fields and
validation, and add tests using the pattern in `tests/test_engine.py`.

## Ground rules

- **Never commit real hosts, IPs, credentials or customer data.** Use `example.com` and the documentation ranges
  `192.0.2.x`, `198.51.100.x` and `203.0.113.x`. The pre-push hook runs [gitleaks](https://github.com/gitleaks/gitleaks)
  and `tools/check_sanitised.py` before anything reaches GitHub. Put your own private terms in a gitignored
  `.sanitise-deny.txt` (see `.sanitise-deny.example.txt`).
- Keep the resident tray process light. Measure memory before and after if you add an import.
- Keep the safety defaults: graceful stop only, no secrets in config, and nothing elevated unless it has to be.
- Add or update tests for engine behaviour, and run `python -m pytest` before opening a pull request.

## Licence

By contributing, you agree that your contributions are licensed under the [MIT licence](LICENSE).
