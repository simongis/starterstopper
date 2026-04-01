# ArcGIS Service Manager

Windows system tray app that starts and stops all ArcGIS Enterprise + ArcGIS Velocity Enterprise services in the correct order, with the required stabilisation waits between each step.

## Tray icon colours

| Colour | Meaning |
|--------|---------|
| Green  | All services running |
| Red    | All services stopped |
| Orange | Some services running (partial) |
| Yellow | Start or stop operation in progress |
| Grey   | Status unknown / checking |

## Service startup order

| # | Service | Wait after start |
|---|---------|-----------------|
| 1 | ArcGIS Data Store | 90 s |
| 2 | ArcGIS Server | 120 s |
| 3 | Portal for ArcGIS | 180 s |
| 4 | ArcGIS Velocity Enterprise Control Plane | 45 s |
| 5 | ArcGIS Velocity Enterprise Gateway | 30 s |
| 6 | ArcGIS Velocity Enterprise Stream Server | 30 s |
| 7 | ArcGIS Velocity Enterprise Feeds | 30 s |
| 8 | ArcGIS Velocity Enterprise Real-Time Analytics | 30 s |
| 9 | ArcGIS Velocity Enterprise Metrics Service | — |

Shutdown is the exact reverse order.

## Prerequisites

- Windows 10 / 11
- Python 3.9+
- ArcGIS Enterprise and ArcGIS Velocity Enterprise installed on this machine

## Installation

```
pip install -r requirements.txt
```

## Running

Right-click `launch.bat` → **Run as administrator**.

> Administrator privileges are required to start and stop Windows services.

All output is written to `arcgis_service_manager.log` in the project root.

### Development / debugging

Run with a visible console to see live log output:

```
python src\main.py
```

(Still requires an elevated prompt.)
