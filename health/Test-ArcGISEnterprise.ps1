# Vendored from the arcgis-enterprise-health skill. Behaviour is unchanged; only example paths were genericised.
# Use with StarterStopper: copy profiles\example.json to a profile outside the repo, then add a health check
# to config.json, for example:
#   { "name": "ArcGIS Enterprise", "format": "json",
#     "cmd": "pwsh -NoProfile -File {app}\\health\\Test-ArcGISEnterprise.ps1 -Profile {config_dir}\\my-deployment.json -Json" }
# With -Json the script prints one JSON object whose "results" array holds {check,status,detail}.
# Exit code = number of FAIL results. Credentials are environment-variable names in the profile, never values.
<#
.SYNOPSIS
  REST-only health probe for any ArcGIS Enterprise deployment (Portal, hosting/federated Servers,
  ArcGIS Data Store, Velocity, Knowledge). Driven by a JSON profile so it is not tied to one environment.

.DESCRIPTION
  Uses only public/admin REST endpoints, so it runs from any machine that can reach the URLs --
  no SSH, no guest access. Credentials come from environment variables named in the profile
  (optionally loaded from a .env file); their values are never printed.

  Status levels: PASS, WARN (degraded or approaching a limit), FAIL (broken or not as expected).
  Exit code = number of FAILs (0 = healthy).

.EXAMPLE
  .\Test-ArcGISEnterprise.ps1 -Profile .\profiles\my-deployment.json
  .\Test-ArcGISEnterprise.ps1 -Profile .\customer.json -Json > result.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Profile,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'
$cfg = Get-Content $Profile -Raw | ConvertFrom-Json
$results = [System.Collections.Generic.List[object]]::new()

function Opt($obj, $name, $default) { if ($obj -and $obj.PSObject.Properties[$name]) { $obj.$name } else { $default } }
function Section($t) { if (-not $Json) { Write-Host ''; Write-Host (("== $t ").PadRight(78, '=')) -ForegroundColor Cyan } }
function Report($name, $status, $detail) {
    $results.Add([pscustomobject]@{ check = $name; status = $status; detail = $detail })
    if (-not $Json) {
        $c = @{ PASS = 'Green'; WARN = 'Yellow'; FAIL = 'Red' }[$status]
        Write-Host ('  {0,-6} {1,-40} {2}' -f $status, $name, $detail) -ForegroundColor $c
    }
}
function DaysUntil([int64]$ms) { [math]::Floor(([DateTimeOffset]::FromUnixTimeMilliseconds($ms) - [DateTimeOffset]::UtcNow).TotalDays) }

$warnDays = [int](Opt $cfg 'warnDays' 60)
$failDays = [int](Opt $cfg 'failDays' 14)
function ExpiryStatus([int]$d) { if ($d -le $failDays) { 'FAIL' } elseif ($d -le $warnDays) { 'WARN' } else { 'PASS' } }

# --- Credentials ---------------------------------------------------------------------------------
$envFile = Opt $cfg 'envFile' $null
if ($envFile -and (Test-Path $envFile)) {
    Get-Content $envFile | Where-Object { $_ -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$' } | ForEach-Object {
        if (-not [Environment]::GetEnvironmentVariable($Matches[1])) {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim().Trim('"').Trim("'"))
        }
    }
}
$user = [Environment]::GetEnvironmentVariable($cfg.credentials.userEnv)
$pass = [Environment]::GetEnvironmentVariable($cfg.credentials.passwordEnv)

$portal = $cfg.portalUrl.TrimEnd('/')
$hdr = @{ Referer = $portal }
$token = $null

function Api($url, $body = @{}, [switch]$Get) {
    $b = @{} + $body; $b.f = 'json'; if ($token) { $b.token = $token }
    $r = if ($Get) { Invoke-RestMethod -Uri $url -Body $b -Headers $hdr -TimeoutSec 30 }
         else      { Invoke-RestMethod -Method Post -Uri $url -Body $b -Headers $hdr -TimeoutSec 30 }
    if ($r.PSObject.Properties['error'] -or ($r.PSObject.Properties['status'] -and $r.status -eq 'error')) {
        $m = if ($r.error) { $r.error.message } else { $r.messages -join '; ' }
        throw "REST error: $m"
    }
    $r
}

# $connectTo: optional IP to dial instead of DNS (for private ports behind a proxy-only DNS name); SNI still uses the host name.
function CertCheck($label, $url, $connectTo) {
    $u = [Uri]$url
    try {
        $tcp = [Net.Sockets.TcpClient]::new(); $tcp.ConnectAsync($(if ($connectTo) { $connectTo } else { $u.Host }), $u.Port).Wait(5000) | Out-Null
        $ssl = [Net.Security.SslStream]::new($tcp.GetStream(), $false, { $true })
        $ssl.AuthenticateAsClient($u.Host)
        $c = [Security.Cryptography.X509Certificates.X509Certificate2]$ssl.RemoteCertificate
        $tcp.Dispose()
        $d = [math]::Floor(($c.NotAfter - (Get-Date)).TotalDays)
        $alg = if ($c.PublicKey.Oid.FriendlyName -match 'ECC') { 'ECC' } else { 'RSA' }
        # Chain trust from this machine's point of view.
        $chain = [Security.Cryptography.X509Certificates.X509Chain]::new()
        $chain.ChainPolicy.RevocationMode = 'NoCheck'
        # Private CAs (internal PKI) listed in the profile are trusted in addition to the OS store.
        $caFiles = @(Opt $cfg 'trustedCaFiles' @() | Where-Object { Test-Path $_ })
        if ($caFiles -and $c.Issuer -ne $c.Subject -and $c.Issuer -notmatch "CN=(YE1|E\d|R\d+)\b") {
            $chain.ChainPolicy.TrustMode = 'CustomRootTrust'
            foreach ($f in $caFiles) { $chain.ChainPolicy.CustomTrustStore.Add([Security.Cryptography.X509Certificates.X509Certificate2]::new($f)) | Out-Null }
        }
        $trusted = $chain.Build($c)
        $nameOk = $c.GetNameInfo('DnsName', $false) -eq $u.Host -or ($c.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.17' } |
                  ForEach-Object { $_.Format($false) }) -match [regex]::Escape($u.Host) -or
                  ($c.Subject -match '\*\.' -and $u.Host -match ('^[^.]+\.' + [regex]::Escape(($c.Subject -replace '.*\*\.([^,]+).*','$1')) + '$'))
        $st = ExpiryStatus $d
        if (-not $trusted -or -not $nameOk) { $st = 'FAIL' }
        Report "cert-$label" $st "$($u.Host):$($u.Port) | $($c.GetNameInfo('SimpleName',$false)) | $($c.GetNameInfo('SimpleName',$true)) | $d days | $alg | trusted=$trusted nameMatch=$([bool]$nameOk)"
    } catch { Report "cert-$label" 'FAIL' "$url : $($_.Exception.Message)" }
}

# =================================================================================================
Section "Portal ($portal)"
try {
    $hc = Invoke-RestMethod "$portal/portaladmin/healthCheck?f=json" -TimeoutSec 20
    Report 'portal-healthCheck' $(if ($hc.status -eq 'success') { 'PASS' } else { 'FAIL' }) "status=$($hc.status)"
} catch { Report 'portal-healthCheck' 'FAIL' $_.Exception.Message }

if (-not $user -or -not $pass) {
    Report 'portal-token' 'FAIL' "credentials not set: env $($cfg.credentials.userEnv) / $($cfg.credentials.passwordEnv) -- admin checks skipped"
} else {
    try {
        $token = (Invoke-RestMethod -Method Post "$portal/sharing/rest/generateToken" -Body @{
            username = $user; password = $pass; client = 'referer'; referer = $portal; expiration = 60; f = 'json' } -TimeoutSec 20).token
        if (-not $token) { throw 'no token returned (wrong credentials or account locked?)' }
        Report 'portal-token' 'PASS' "admin token issued for $user"
    } catch { Report 'portal-token' 'FAIL' $_.Exception.Message }
}

if ($token) {
    try {
        $pa = Api "$portal/portaladmin" -Get
        $want = Opt $cfg 'expectedVersion' $null
        Report 'portal-version' $(if (-not $want -or $pa.version -like "$want*") { 'PASS' } else { 'WARN' }) "version=$($pa.version)$(if ($want) { " (expected $want)" })"
    } catch { Report 'portal-version' 'WARN' $_.Exception.Message }

    try {
        $lic = Api "$portal/portaladmin/license" -Get
        Report 'portal-members' $(if ($lic.currentRegisteredMembers -lt $lic.maximumRegisteredMembers) { 'PASS' } else { 'WARN' }) "$($lic.currentRegisteredMembers) of $($lic.maximumRegisteredMembers) registered members"
        foreach ($ut in @($lic.userTypes) + @($lic.extensions)) {
            if (-not $ut.expiration) { continue }
            $d = DaysUntil $ut.expiration
            Report "portal-licence-$($ut.id)" (ExpiryStatus $d) "expires $(([DateTimeOffset]::FromUnixTimeMilliseconds($ut.expiration)).LocalDateTime.ToString('yyyy-MM-dd')) ($d days)$(if ($null -ne $ut.currentRegisteredMembers) { ", $($ut.currentRegisteredMembers)/$($ut.maximumRegisteredMembers) used" })"
        }
    } catch { Report 'portal-licence' 'WARN' $_.Exception.Message }

    try {
        $fv = Api "$portal/portaladmin/federation/servers/validate" -Get
        $fs = (Api "$portal/portaladmin/federation/servers" -Get).servers
        foreach ($s in $fv.serversStatus) {
            $meta = $fs | Where-Object { $_.id -eq $s.serverId }
            $role = "$($meta.serverRole)$(if ($meta.serverFunction) { "/$($meta.serverFunction)" })"
            Report "federation-$(([Uri]$meta.url).Host)" $(if ($s.status -eq 'success') { 'PASS' } else { 'FAIL' }) "$role $($meta.url) -- $(if ($s.status -eq 'success') { 'validated' } else { $s.messages -join ' | ' })"
        }
        foreach ($exp in @(Opt $cfg 'expectedFederatedServers' @())) {
            if (-not ($fs | Where-Object { $_.url.TrimEnd('/') -eq $exp.TrimEnd('/') })) { Report "federation-expected" 'FAIL' "$exp is not federated" }
        }
    } catch { Report 'federation' 'FAIL' $_.Exception.Message }

    $logHours = [int](Opt $cfg 'logHours' 24)
    try {
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $pl = Api "$portal/portaladmin/logs/query" @{ level = 'SEVERE'; startTime = $now; endTime = $now - $logHours * 3600000; pageSize = 100 }
        $n = @($pl.logMessages).Count
        $sample = if ($n) { $lm = $pl.logMessages[0]; " -- latest $(([DateTimeOffset]::FromUnixTimeMilliseconds([int64]$lm.time)).LocalDateTime.ToString('dd MMM HH:mm')): " + ($lm.message -replace '\s+', ' ').Substring(0, [math]::Min(110, $lm.message.Length)) }
        Report 'portal-logs-severe' $(if ($n -eq 0) { 'PASS' } else { 'WARN' }) "$n SEVERE in last ${logHours}h$sample"
    } catch { Report 'portal-logs-severe' 'WARN' $_.Exception.Message }
}

# =================================================================================================
foreach ($srv in @($cfg.servers)) {
    $s = $srv.url.TrimEnd('/')
    Section "Server: $($srv.name) ($s)"
    try {
        $hc = Invoke-RestMethod "$s/rest/info/healthCheck?f=json" -TimeoutSec 20
        Report "$($srv.name)-healthCheck" $(if ($hc.success) { 'PASS' } else { 'FAIL' }) "success=$($hc.success)"
    } catch { Report "$($srv.name)-healthCheck" 'FAIL' $_.Exception.Message }
    if (-not $token) { continue }

    try {
        foreach ($m in (Api "$s/admin/machines").machines) {
            $st = Api "$s/admin/machines/$($m.machineName)/status"
            Report "$($srv.name)-machine-$($m.machineName)" $(if ($st.realTimeState -eq 'STARTED') { 'PASS' } else { 'FAIL' }) "configured=$($st.configuredState) realTime=$($st.realTimeState)"
        }
    } catch { Report "$($srv.name)-machines" 'FAIL' $_.Exception.Message }

    try {
        $lic = Api "$s/admin/system/licenses"
        if ($lic.edition.expiration) {
            $d = DaysUntil $lic.edition.expiration
            Report "$($srv.name)-licence" (ExpiryStatus $d) "$($lic.edition.name) $($lic.edition.version) expires in $d days"
        }
    } catch { Report "$($srv.name)-licence" 'WARN' $_.Exception.Message }

    # Services whose configured state is STARTED but are not actually running.
    try {
        $root = Api "$s/admin/services"
        $bad = @(); $total = 0; $stopped = 0
        foreach ($folder in @('') + @($root.folders | Where-Object { $_ -notin 'System', 'Utilities' -or $srv.includeSystemFolders })) {
            $rep = Api ("$s/admin/services" + $(if ($folder) { "/$folder" }) + '/report')
            foreach ($r in $rep.reports) {
                $total++
                if ($r.status.configuredState -eq 'STOPPED') { $stopped++ }
                elseif ($r.status.realTimeState -ne 'STARTED') { $bad += "$folder/$($r.serviceName).$($r.type)=$($r.status.realTimeState)" }
            }
        }
        Report "$($srv.name)-services" $(if (-not $bad) { 'PASS' } else { 'FAIL' }) "$total services, $stopped stopped by config$(if ($bad) { '; NOT RUNNING: ' + ($bad -join ', ') })"
    } catch { Report "$($srv.name)-services" 'WARN' $_.Exception.Message }

    # Registered data stores: every managed item validated machine by machine.
    if ($srv.checkDataStores) {
        $found = @{}
        foreach ($pp in '/enterpriseDatabases', '/nosqlDatabases', '/cloudStores') {
            try {
                foreach ($it in (Api "$s/admin/data/findItems" @{ parentPath = $pp }).items) {
                    if (-not $it.info.isManaged) { continue }
                    $kind = switch -Regex ($it.path) {
                        'objectstore' { 'object' } '_bigdata_' { 'spatiotemporal' } '_graph_' { 'graph' }
                        '_queue_' { 'queue' } '_cache_' { 'cache' } '^/enterpriseDatabases' { 'relational' } default { 'other' } }
                    $found[$kind] = $true
                    $ms = @()
                    try {
                        $ms = (Api "$s/admin/data/items$($it.path)/machines").machines
                        $msgs = foreach ($mm in $ms) {
                            $v = Api "$s/admin/data/items$($it.path)/machines/$($mm.name)/validate"
                            if ($v.status -ne 'success') { "$($mm.name): $($v.messages -join ' ')" }
                        }
                        Report "$($srv.name)-datastore-$kind" $(if (-not $msgs) { 'PASS' } else { 'FAIL' }) "$($it.path) on $(($ms.name) -join ',')$(if ($msgs) { ' -- ' + ($msgs -join '; ') } else { ' validated' })"
                    } catch {
                        $m = $_.Exception.Message
                        # Queue and in-memory cache stores (11.4+) have no usable per-machine validate; a listed,
                        # activated machine is the best REST evidence. Anything else is a real failure.
                        if ($kind -in 'queue', 'cache' -and $ms -and -not ($ms | Where-Object { $_.PSObject.Properties['activated'] -and -not $_.activated })) {
                            Report "$($srv.name)-datastore-$kind" 'WARN' "$($it.path) on $(($ms.name) -join ',') -- registered, validate not supported: $($m.Substring(0, [math]::Min(110, $m.Length)))"
                        } else { Report "$($srv.name)-datastore-$kind" 'FAIL' "$($it.path): $m" }
                    }
                }
            } catch { Report "$($srv.name)-datastores$pp" 'WARN' $_.Exception.Message }
        }
        foreach ($k in @($srv.expectedDataStores)) {
            if ($k -and -not $found[$k]) { Report "$($srv.name)-datastore-$k" 'FAIL' 'expected but not registered as a managed data store' }
        }
    }

    try {
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $lh = [int](Opt $cfg 'logHours' 24)
        $sl = Api "$s/admin/logs/query" @{ level = 'SEVERE'; startTime = $now; endTime = $now - $lh * 3600000; pageSize = 100; filter = '{"server":"*","services":"*","machines":"*"}' }
        $n = @($sl.logMessages).Count
        $sample = if ($n) { $lm = $sl.logMessages[0]; " -- latest $(([DateTimeOffset]::FromUnixTimeMilliseconds([int64]$lm.time)).LocalDateTime.ToString('dd MMM HH:mm')) $($lm.source): " + ($lm.message -replace '\s+', ' ').Substring(0, [math]::Min(100, $lm.message.Length)) }
        Report "$($srv.name)-logs-severe" $(if ($n -eq 0) { 'PASS' } else { 'WARN' }) "$n SEVERE in last ${lh}h$(if ($sl.hasMore) { '+' })$sample"
    } catch { Report "$($srv.name)-logs-severe" 'WARN' $_.Exception.Message }
}

# =================================================================================================
if ($cfg.PSObject.Properties['velocity'] -and $cfg.velocity) {
    $v = $cfg.velocity.url.TrimEnd('/')
    Section "Velocity ($v)"
    if ($token) {
        $vb = @{ token = $token }
        try {
            $feeds = Invoke-RestMethod "$v/iot/feed?f=json&token=$token" -Headers $hdr -TimeoutSec 30
            foreach ($f in $feeds) {
                $want = -not $cfg.velocity.requireStarted -or ($f.label -in $cfg.velocity.requireStarted)
                $st = if ($f.status.itemState -eq 'started') { 'PASS' } elseif ($want) { 'FAIL' } else { 'WARN' }
                Report "velocity-feed-$($f.label)" $st "itemState=$($f.status.itemState)"
            }
            $an = Invoke-RestMethod "$v/iot/analytics/realtime?f=json&token=$token" -Headers $hdr -TimeoutSec 30
            foreach ($a in $an) {
                $val = Invoke-RestMethod "$v/iot/analytics/realtime/validate/$($a.id)?f=json&token=$token" -Headers $hdr -TimeoutSec 60
                $all = @($val.validation.messages) + @($val.nodes | ForEach-Object { $_.validation.messages })
                $errs = @($all | Where-Object { $_ -and $_.level -eq 'ERROR' })
                $want = -not $cfg.velocity.requireStarted -or ($a.label -in $cfg.velocity.requireStarted)
                $st = if ($errs -or -not $val.executable) { 'FAIL' } elseif ($a.status.itemState -eq 'started') { 'PASS' } elseif ($want) { 'FAIL' } else { 'WARN' }
                Report "velocity-analytic-$($a.label)" $st "itemState=$($a.status.itemState), executable=$($val.executable), errors=$($errs.Count) warnings=$(@($all | Where-Object { $_ -and $_.level -eq 'WARN' }).Count)"
            }
        } catch { Report 'velocity-items' 'FAIL' $_.Exception.Message }
    }
}

# =================================================================================================
$probes = @(Opt $cfg 'probeLayers' @())
if ($probes) {
    Section 'Layer probes (query returns rows)'
    foreach ($pl in $probes) {
        try {
            $r = Invoke-RestMethod "$($pl.url.TrimEnd('/'))/query?where=1%3D1&returnCountOnly=true&f=json$(if ($token) { "&token=$token" })" -Headers $hdr -TimeoutSec 30
            if ($r.error) { throw $r.error.message }
            Report "layer-$($pl.name)" $(if ($r.count -ge [int](Opt $pl 'minCount' 1)) { 'PASS' } else { 'WARN' }) "count=$($r.count)"
        } catch { Report "layer-$($pl.name)" 'FAIL' $_.Exception.Message }
    }
}

# =================================================================================================
Section 'Certificates'
# extraCertUrls entries are a URL string or { "url": ..., "connectTo": "<ip>" }.
$certUrls = @($portal) + @($cfg.servers | ForEach-Object { $_.url }) + @(Opt $cfg 'extraCertUrls' @())
if ($cfg.PSObject.Properties['velocity'] -and $cfg.velocity) { $certUrls += $cfg.velocity.url }
$seen = @{}
foreach ($e in $certUrls) {
    $u = if ($e -is [string]) { $e } else { $e.url }
    $k = ([Uri]$u).Authority
    if ($seen[$k]) { continue }; $seen[$k] = $true
    CertCheck $k $u $(if ($e -isnot [string]) { Opt $e 'connectTo' $null })
}

# =================================================================================================
$fails = @($results | Where-Object status -eq 'FAIL').Count
$warns = @($results | Where-Object status -eq 'WARN').Count
if ($Json) {
    [pscustomobject]@{ profile = (Resolve-Path $Profile).Path; checkedAt = (Get-Date).ToString('o'); fail = $fails; warn = $warns; results = $results } |
        ConvertTo-Json -Depth 4
} else {
    Write-Host ''; Write-Host ('=' * 78)
    Write-Host "$fails FAIL, $warns WARN" -ForegroundColor $(if ($fails) { 'Red' } elseif ($warns) { 'Yellow' } else { 'Green' })
    Write-Host ('=' * 78)
}
exit $fails
