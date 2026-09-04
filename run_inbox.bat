@echo off
REM Owner commands only. Runs often and cheaply so a DO/HOLD is never sitting
REM unread while a deadline passes -- the full analysis stays on its own slower
REM schedule in run_daily.bat.
cd /d "%~dp0"
python fpl_token.py --refresh >> "%~dp0state\run.log" 2>&1
python fpl_inbox.py --apply   >> "%~dp0state\run.log" 2>&1
