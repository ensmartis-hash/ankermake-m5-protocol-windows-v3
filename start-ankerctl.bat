@echo off
title ankerctl - http://localhost:4470
cd /d C:\Users\dell\ankermake-m5-protocol
set PATH=C:\Users\dell\AppData\Local\Programs\Python\Python312;C:\Users\dell\AppData\Local\Programs\Python\Python312\Scripts;%PATH%
echo.
echo  ankerctl is starting...
echo  Open: http://localhost:4470
echo  Keep this window open while printing from Orca.
echo  Close this window to stop ankerctl.
echo.
echo  Tip: After the PC wakes from sleep, click Test in Orca once
echo  (or open the web UI). ankerctl will refresh PPPP automatically.
echo.
:loop
python -u ankerctl.py webserver run --host 0.0.0.0 --port 4470
echo.
echo  ankerctl stopped. Restarting in 3 seconds... (Ctrl+C to quit)
timeout /t 3 /nobreak >nul
goto loop
