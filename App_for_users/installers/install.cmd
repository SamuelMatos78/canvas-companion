@echo off
setlocal
set "INSTALL_SCRIPT=%~dp0install.ps1"
if not exist "%INSTALL_SCRIPT%" (
  echo O arquivo install.ps1 precisa ficar na mesma pasta deste instalador.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_SCRIPT%"
set "INSTALL_EXIT=%ERRORLEVEL%"
if not "%INSTALL_EXIT%"=="0" (
  echo.
  echo A instalacao terminou com erro. Consulte a mensagem acima.
  pause
)
exit /b %INSTALL_EXIT%
