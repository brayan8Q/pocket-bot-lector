@echo off
rem Abre la terminal de prueba PUBLICA de Pocket Option (sin usuario ni contrasena) y guarda esa sesion DEMO anonima.
rem Sirve para correr el bot en otro equipo sin la cuenta del dueno. Solo DEMO.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m pocket_tyl.capturar_ssid --publica
