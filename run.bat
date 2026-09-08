@echo off
REM Starts the API and the web interface in two windows.
cd /d "%~dp0"

echo Installing backend dependencies...
python -m pip install -q -r backend\requirements.txt

if not exist frontend\node_modules (
  echo Installing frontend dependencies...
  pushd frontend && call npm install && popd
)

start "Fact Knowledge Layer API" cmd /k "cd /d %~dp0backend && python -m uvicorn app.main:app --port 8000"
start "Fact Knowledge Layer Web" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo Web interface  http://localhost:5173
echo API reference  http://localhost:8000/docs
