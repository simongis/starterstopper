from dataclasses import dataclass
from typing import List


@dataclass
class ServiceConfig:
    name: str
    wait_after_start: int  # seconds to wait after start before proceeding to next service


# Order matters: startup proceeds top-to-bottom, shutdown is exact reverse.
# Windows SCM has no dependency chain between these, so we manage order and
# waits ourselves.
SERVICES: List[ServiceConfig] = [
    ServiceConfig("ArcGIS Data Store",                              90),
    ServiceConfig("ArcGIS Server",                                 120),
    ServiceConfig("Portal for ArcGIS",                             180),
    ServiceConfig("ArcGIS Velocity Enterprise Control Plane",       45),
    ServiceConfig("ArcGIS Velocity Enterprise Gateway",             30),
    ServiceConfig("ArcGIS Velocity Enterprise Stream Server",       30),
    ServiceConfig("ArcGIS Velocity Enterprise Feeds",               30),
    ServiceConfig("ArcGIS Velocity Enterprise Real-Time Analytics", 30),
    ServiceConfig("ArcGIS Velocity Enterprise Metrics Service",      0),
]
