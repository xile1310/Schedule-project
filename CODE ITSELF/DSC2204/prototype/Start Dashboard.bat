@echo off
title Scheduler Dashboard
cd /d "%~dp0"
echo Starting dashboard at http://localhost:8501 ...
echo Close this window to stop the server.
"c:\Users\cheng\Desktop\Scheduler project\.venv\Scripts\streamlit.exe" run app.py --server.port 8501
pause
