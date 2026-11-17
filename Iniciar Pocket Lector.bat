@echo off
rem Puente del Lector de tendencia a la DEMO de Pocket Option (usuario 15 nov 2026). Proceso propio: no toca el bot de oro ni MT5.
rem SOLO cuenta DEMO. Abre las operaciones que marca el panel "Lector de tendencia" (senal y su martingala).
rem Para detenerlo: cierra la ventana "Pocket Lector DEMO". Deja copia de lo que muestra en artifacts\pocket_lector.log
cd /d "%~dp0"
set POCKET_LECTOR_LOG=%~dp0artifacts\pocket_lector.log
start "Pocket Lector DEMO" ".venv\Scripts\python.exe" -m pocket_tyl.lector
