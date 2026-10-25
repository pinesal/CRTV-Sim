@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Rollback-CRTV-Sim-0.9.57.ps1"
if errorlevel 1 pause
