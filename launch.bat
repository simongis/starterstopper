@echo off
:: Run as Administrator — service control requires elevated privileges.
:: Uses pythonw.exe so no console window appears; all output goes to
:: arcgis_service_manager.log in the project root.
cd /d "%~dp0"
pythonw src\main.py
