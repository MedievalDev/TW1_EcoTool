@echo off
rem Starts the built exe in self-test mode and shows the line (PY_TOOL_DESIGN.md 7.6).
set "ECOTOOL_SELFTEST=%TEMP%\ecotool_selftest.txt"
del "%ECOTOOL_SELFTEST%" 2>nul
start "" /wait "%~dp0dist\TW1_EcoTool.exe"
ping -n 4 127.0.0.1 >nul
type "%ECOTOOL_SELFTEST%"
