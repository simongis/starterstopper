# Health probe

`Test-ArcGISEnterprise.ps1` is a REST-only health probe for ArcGIS Enterprise (Portal, hosting and federated
Servers, ArcGIS Data Store, Velocity). It needs PowerShell 7 (`pwsh`), HTTPS access to the URLs and a Portal
administrator account. It is vendored from the `arcgis-enterprise-health` skill.

## What it checks

- Portal: healthCheck, admin token, version, member count, licence expiry, recent SEVERE log entries.
- Federation: each federated server validates; expected servers are present.
- Each Server: healthCheck, machine state, licence expiry, services configured STARTED but not running, SEVERE logs.
- Data Store: each managed store validates per machine; expected store kinds are registered.
- Velocity (optional): feed and analytic state, analytic validation errors.
- Layers (optional): a query returns at least `minCount` rows.
- Certificates: expiry, name match and chain trust for each URL.

Each result is PASS, WARN or FAIL. The exit code is the number of FAILs. With `-Json` the output is one JSON
object whose `results` array holds `{check, status, detail}`.

## Writing a profile

Copy `profiles/example.json` to a location outside the repository (for example next to your StarterStopper
`config.json`) and edit it:

- `portalUrl`, `servers[]` (`name`, `url`, `checkDataStores`, `expectedDataStores`), `expectedFederatedServers`.
- `velocity` (`url`, `requireStarted` labels) or `null`.
- `warnDays` / `failDays` for certificate and licence expiry, `logHours` for the log window.
- `extraCertUrls` for extra endpoints; use `connectTo` to dial an IP when DNS does not route that port.
- `trustedCaFiles` for internal certificate authorities not in the Windows store.

## Credentials

Never put a username or password in a profile. The profile names two environment variables
(`credentials.userEnv`, `credentials.passwordEnv`). Set them in the environment, or set `envFile` to a
`NAME=value` file kept outside the repository; values already in the environment win. The script never prints them.

## Using it from StarterStopper

Add a check to `config.json` (`health.checks`):

```json
{ "name": "ArcGIS Enterprise", "format": "json",
  "cmd": "pwsh -NoProfile -File {app}\health\Test-ArcGISEnterprise.ps1 -Profile {config_dir}\my-deployment.json -Json" }
```
