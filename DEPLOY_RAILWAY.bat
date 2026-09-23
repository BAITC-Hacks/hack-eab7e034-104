@echo off
cd /d "%~dp0"
where railway >nul 2>nul
if errorlevel 1 (
  echo Railway CLI not found. Installing...
  npm install -g @railway/cli
  if errorlevel 1 goto :error
)
echo.
echo Deploying StockPilot AI to a NEW Railway project...
railway up --new --name stockpilot-ai
if errorlevel 1 goto :error
echo.
echo Creating public domain...
railway domain
echo.
echo DONE. Open the *.up.railway.app address shown above.
pause
exit /b 0
:error
echo.
echo Deployment failed. Copy the last error lines and send them to ChatGPT.
pause
exit /b 1
