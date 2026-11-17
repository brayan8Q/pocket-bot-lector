@echo off
rem Abre una ventana aparte para iniciar sesion en Pocket Option y capturar el SSID de la cuenta DEMO. No toca el bot de oro.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m pocket_tyl.capturar_ssid
