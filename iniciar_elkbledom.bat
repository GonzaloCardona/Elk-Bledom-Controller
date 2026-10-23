@echo off
title ELK-BLEDOM LED Controller
color 0A
echo.
echo  ==========================================
echo    ELK-BLEDOM LED Controller
echo    Comprobando dependencias...
echo  ==========================================
echo.

:: Comprobar Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python no encontrado.
    echo  Descargalo desde https://www.python.org/downloads/
    echo  y asegurate de marcar "Add Python to PATH"
    pause
    exit /b 1
)

:: Comprobar e instalar dependencias si faltan
python -c "import bleak, pyaudiowpatch, numpy, mss, pystray, PIL" >nul 2>&1
if errorlevel 1 (
    echo  Instalando dependencias desde requirements.txt...
    pip install -r requirements.txt --quiet
)

:: Iniciar la app
echo  Iniciando ELK-BLEDOM Controller...
echo.
python elkbledom_controller.py

if errorlevel 1 (
    echo.
    echo  La aplicacion cerro con un error.
    pause
)
