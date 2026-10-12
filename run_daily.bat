@echo off
cd /d "%~dp0"
python fpl_token.py --refresh    >> "%~dp0state\run.log" 2>&1
python fpl_inbox.py --apply      >> "%~dp0state\run.log" 2>&1
REM News layer. Runs on the Claude subscription, not the API, so it costs nothing
REM per call -- which is why it can finally run daily instead of never. --soft so
REM a news outage can never stop the engine managing the team; --max-age-hours so
REM three scheduled runs a day do not mean three sets of web searches.
python fpl_judge.py --via-claude-code --soft --max-age-hours 10 >> "%~dp0state\run.log" 2>&1
python fpl_daily.py --apply      >> "%~dp0state\run.log" 2>&1
python fpl_email.py --send --once-daily >> "%~dp0state\run.log" 2>&1
