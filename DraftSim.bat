@echo off
cd /d "%~dp0"
start "" python limitedtool.py
python draft_sim.py %*
pause
