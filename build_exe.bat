@echo off
rem Builds TW1 EcoTool as one exe (PyInstaller, onefile, no console).
rem Result: %~dp0dist\TW1_EcoTool.exe
setlocal
pushd "%~dp0"
py -3.13 -m PyInstaller --noconfirm --onefile --windowed ^
  --name "TW1_EcoTool" ^
  --add-data "%~dp0untested.json;." ^
  --add-data "%~dp0decomp;decomp" ^
  --hidden-import theme --hidden-import guidebook --hidden-import updater --hidden-import version ^
  --hidden-import ecocore --hidden-import i18n_de --hidden-import foxfeedback --hidden-import foxfeedback_ui ^
  --hidden-import capstone ^
  --collect-all capstone ^
  --distpath "%~dp0dist" --workpath "%TEMP%\ecotool_build" --specpath "%TEMP%\ecotool_build" eco_tool.py
set rc=%errorlevel%
popd
if %rc% neq 0 (echo BUILD FAILED & exit /b %rc%)
echo BUILD OK: %~dp0dist\TW1_EcoTool.exe
