@echo off
setlocal
if exist "%~dp0Windows\WaraqStudio\WaraqStudio.exe" (
    start "" "%~dp0Windows\WaraqStudio\WaraqStudio.exe"
) else (
    pythonw "%~dp0main.py"
)
