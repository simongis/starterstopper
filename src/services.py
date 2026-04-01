from dataclasses import dataclass
from typing import List


@dataclass
class ServiceConfig:
    name: str          # display name shown in logs and UI
    service_name: str  # internal Windows service name used for sc/PowerShell control
    wait_after_start: int  # seconds to wait after start before proceeding to next service


# Order matters: startup proceeds top-to-bottom, shutdown is exact reverse.
# Windows SCM has no dependency chain between these, so we manage order and
# waits ourselves.
SERVICES: List[ServiceConfig] = [
    ServiceConfig("ArcGIS Data Store",                              "ArcGIS Data Store",                          90),
    ServiceConfig("ArcGIS Server",                                  "ArcGIS Server",                             120),
    ServiceConfig("Portal for ArcGIS",                              "Portal for ArcGIS",                         180),
    ServiceConfig("ArcGIS Velocity Enterprise Control Plane",       "ArcGISVelocityEnterpriseControlPlane",       45),
    ServiceConfig("ArcGIS Velocity Enterprise Gateway",             "ArcGISVelocityEnterpriseGateway",            30),
    ServiceConfig("ArcGIS Velocity Enterprise Stream Server",       "ArcGISVelocityEnterpriseStreamServer",       30),
    ServiceConfig("ArcGIS Velocity Enterprise Feeds",               "ArcGISVelocityEnterpriseFeeds",              30),
    ServiceConfig("ArcGIS Velocity Enterprise Real-Time Analytics", "ArcGISVelocityEnterpriseRealtimeAnalytics",  30),
    ServiceConfig("ArcGIS Velocity Enterprise Metrics Service",     "ArcGISVelocityEnterpriseMetrics",             0),
]
