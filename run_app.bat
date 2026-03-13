@echo off
echo ==========================================
echo   Starting Garmin City Explorer
echo ==========================================
echo.

:: Check if node_modules exists in frontend
if not exist "frontend\node_modules\" (
    echo [!] Frontend dependencies not found. 
    echo [!] Running 'npm install' in frontend directory first...
    cd frontend && call npm install && cd ..
)

echo [+] Starting Backend (FastAPI) in a new window...
start "Garmin Backend" cmd /k "python -m backend.main"

echo [+] Starting Frontend (React) in a new window...
start "Garmin Frontend" cmd /k "cd frontend && npm start"

echo.
echo ------------------------------------------
echo Backend will be at: http://localhost:8000
echo Frontend will be at: http://localhost:3000
echo ------------------------------------------
echo.
echo Close the individual terminal windows to stop the servers.
pause
