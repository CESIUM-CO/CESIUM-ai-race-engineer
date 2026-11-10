@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "OUT=%~dp0dist_debug"

echo [BUILD] Checking environment...
if not exist "%PY%" goto novenv
if not exist "%~dp0main_pitwall.py" goto nomain
if not exist "%~dp0boot_guard.py" goto noguard
if not exist "%~dp0ac_app\PitwallSpotter\PitwallSpotter.py" goto nospotter

echo [BUILD] Locating espeak-ng (needed by the natural voice)...
set "ESP="
set "HELP=%TEMP%\cesium_find_espeak.py"
set "OUTF=%TEMP%\cesium_find_espeak.txt"
> "%HELP%" echo import espeakng_loader, os
>> "%HELP%" echo print(os.path.dirname(espeakng_loader.__file__))
if exist "%OUTF%" del "%OUTF%"
"%PY%" "%HELP%" > "%OUTF%" 2>nul
if errorlevel 1 goto noesploader
set /p ESP=<"%OUTF%"
if "%ESP%"=="" goto noesploader
if not exist "%ESP%\espeak-ng.dll" goto noespdll
if not exist "%ESP%\espeak-ng-data" goto noespdata
echo [BUILD] espeak-ng found in: %ESP%

echo [BUILD] DEBUG build: visible console + compilation report.
echo [BUILD] Output: %OUT%\main_pitwall.dist

"%PY%" -m nuitka --standalone ^
  --enable-plugin=tk-inter ^
  --windows-console-mode=force ^
  --output-dir="%OUT%" ^
  --report="%OUT%\compilation-report.xml" ^
  --include-module=boot_guard ^
  --include-module=llm_worker --include-module=paths ^
  --include-module=setup_advisor --include-module=track_intel ^
  --include-module=incident_monitor --include-module=corner_coach ^
  --include-module=credentials --include-module=account_manager ^
  --include-module=cloud_coach ^
  --include-module=updater ^
  --include-module=pystray._win32 ^
  --include-package=llama_cpp --include-package-data=llama_cpp ^
  --include-package=faster_whisper --include-package-data=faster_whisper ^
  --include-package=ctranslate2 --include-package=onnxruntime ^
  --include-package=kokoro_onnx --include-package-data=kokoro_onnx ^
  --include-package=espeakng_loader ^
  "--include-data-files=%ESP%\espeak-ng.dll=espeakng_loader\espeak-ng.dll" ^
  "--include-data-dir=%ESP%\espeak-ng-data=espeakng_loader\espeak-ng-data" ^
  --include-package-data=language_tags --include-package-data=phonemizer ^
  --include-package-data=certifi ^
  --include-data-dir=ac_app=ac_app ^
  --include-data-files=ac_app\PitwallSpotter\PitwallSpotter.py=ac_app\PitwallSpotter\PitwallSpotter.py ^
  --include-data-files=ac_app\PitwallSpotter\manifest.ini=ac_app\PitwallSpotter\manifest.ini ^
  --include-data-files=cesium.ico=cesium.ico ^
  --windows-icon-from-ico=cesium.ico ^
  --output-filename=CesiumAIRaceEngineer.exe ^
  main_pitwall.py

if errorlevel 1 goto failed

echo.
echo [BUILD] Finished successfully.
echo.
echo NEXT STEPS:
echo    cd "%OUT%\main_pitwall.dist"
echo    set CESIUM_NO_UPDATE=1
echo    CesiumAIRaceEngineer.exe --selftest
echo.
echo Logs: %%APPDATA%%\CesiumAIRaceEngineer\logs\boot.log and cesium.log
goto end

:novenv
echo.
echo [ERROR] .venv\Scripts\python.exe was not found next to this script.
echo         Expected: %PY%
goto end

:nomain
echo.
echo [ERROR] main_pitwall.py was not found next to this script.
goto end

:noguard
echo.
echo [ERROR] boot_guard.py was not found next to this script.
goto end

:nospotter
echo.
echo [ERROR] ac_app\PitwallSpotter\PitwallSpotter.py was not found.
echo         Create the folders ac_app\PitwallSpotter next to this script and put the file inside.
goto end

:noesploader
echo.
echo [ERROR] The package espeakng_loader is not installed in .venv (or cannot be imported).
echo         Run:  "%PY%" -m pip install espeakng-loader kokoro-onnx
goto end

:noespdll
echo.
echo [ERROR] espeak-ng.dll was not found in: %ESP%
echo         Reinstall it:  "%PY%" -m pip install --force-reinstall espeakng-loader
goto end

:noespdata
echo.
echo [ERROR] The espeak-ng-data folder was not found in: %ESP%
echo         Reinstall it:  "%PY%" -m pip install --force-reinstall espeakng-loader
goto end

:failed
echo.
echo [ERROR] The build failed. Read the messages above.

:end
echo.
pause
endlocal
