@echo off
setlocal EnableExtensions
rem Usage: post_install.bat "<install dir>"      (called hidden by cesium_setup.iss)
set "APPDIR=%~1"
set "LOG=%TEMP%\cesium_postinstall.log"
echo [%date% %time%] post-install started for "%APPDIR%" > "%LOG%"

rem ---- 1. Visual C++ 2015-2022 x64 runtime (needed by onnxruntime / ctranslate2 / llama.cpp) ----
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed >nul 2>&1
if errorlevel 1 (
    if exist "%~dp0vc_redist.x64.exe" (
        echo VC++ runtime missing: installing bundled redistributable >> "%LOG%"
        "%~dp0vc_redist.x64.exe" /install /quiet /norestart
        echo   exit code %errorlevel% ^(3010 = reboot recommended^) >> "%LOG%"
    ) else (
        echo WARNING: VC++ runtime missing and no redistributable bundled >> "%LOG%"
    )
) else (
    echo VC++ runtime present >> "%LOG%"
)

rem ---- 2. Sanity check of the critical files in the install folder ----
for %%F in ("CesiumAIRaceEngineer.exe" "cesium.ico" "espeakng_loader\espeak-ng.dll" ^
            "ac_app\PitwallSpotter\PitwallSpotter.py") do (
    if exist "%APPDIR%\%%~F" ( echo OK      %%~F >> "%LOG%" ) else ( echo MISSING %%~F >> "%LOG%" )
)

rem ---- 3. Remove leftovers of an interrupted self-update ----
if exist "%APPDIR%\_update\staging" rd /s /q "%APPDIR%\_update\staging" >nul 2>&1

echo [%date% %time%] post-install finished >> "%LOG%"
exit /b 0
