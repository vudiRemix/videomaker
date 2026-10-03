@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Как пользоваться:
rem   - двойной клик: обработать все видео из папки input
rem   - перетащить видео на этот файл: обработать именно их
rem Готовые части появятся в папке output.

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (python --version >nul 2>&1 && set "PY=python")
if not defined PY (
    echo Python не найден.
    choice /M "Установить Python автоматически через winget"
    if errorlevel 2 goto end
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    echo.
    echo Python установлен. Закройте это окно и запустите run.bat ещё раз.
    goto end
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg не найден.
    choice /M "Установить ffmpeg автоматически через winget"
    if errorlevel 2 goto end
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    echo.
    echo ffmpeg установлен. Закройте это окно и запустите run.bat ещё раз.
    goto end
)

%PY% "%~dp0split_with_banner.py" %*

:end
echo.
pause
