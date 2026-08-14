#!/usr/bin/env python3
"""
First-run setup. Walks you through connecting your FPL account.

    python fpl_setup.py

Checks dependencies, explains the risk, gets your refresh token, verifies it
against the live API, and tells you what to run next.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"

RULE = "─" * 72


def head(t):
    print(f"\n{RULE}\n  {t}\n{RULE}")


def main():
    head("FPL AUTO-MANAGER — SETUP")

    # ---- dependencies
    missing = []
    for mod, pkg in (("requests", "requests"), ("pulp", "pulp")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        sys.exit(f"Missing packages: {', '.join(missing)}\n"
                 f"Run:  pip install -r requirements.txt")
    print("dependencies ....... ok")

    # ---- the warning, stated plainly
    head("READ THIS FIRST")
    print("""This tool makes REAL CHANGES to your Fantasy Premier League team:
it can change your lineup, your captain, and make transfers, without
asking you at the time.

Two things you are agreeing to:

  1. BAN RISK. FPL's terms restrict automated access. Managers have been
     banned for automating team management. That risk lands on YOUR
     account, not on whoever wrote this code. Nobody can promise you it
     is safe. If losing your team would upset you, stop here and run it
     with --dry-run instead, which only ever reports.

  2. CREDENTIAL RISK. Setup stores an OIDC refresh token in state/auth.json.
     It is valid for roughly 180 days and anyone who obtains it can read and
     MODIFY your team until you log out of Premier League everywhere.
     Never commit it, never sync state/ to cloud storage, never paste it
     into a chat or an issue.

You keep your own credential. It never leaves this machine.""")

    if input("\nType 'i understand' to continue: ").strip().lower() != "i understand":
        sys.exit("Setup cancelled. Nothing was written.")

    # ---- token
    import fpl_token
    import fpl_write

    head("STEP 1 — CONNECT YOUR ACCOUNT")
    existing = fpl_token._read().get("refresh_token")
    if existing:
        print("A refresh token is already stored.")
        if input("Replace it? [y/N]: ").strip().lower() != "y":
            print("Keeping the existing token.")
        else:
            existing = None

    if not existing:
        print("""
  1. Log in at  https://fantasy.premierleague.com
  2. Press F12, open the Console tab
  3. If it says pasting is blocked, type:  allow pasting
  4. Paste this one line and press Enter:
""")
        print(f"     {fpl_token.CONSOLE_SNIPPET}\n")
        print("  It copies your refresh token to the clipboard and prints the")
        print("  character count. If it prints 'NO refresh_token found', you are")
        print("  not logged in - log in and try again.\n")
        raw = input("Paste the token here: ").strip()
        if not raw:
            sys.exit("Nothing pasted. Setup cancelled.")
        fpl_token.save_refresh(raw)
        if not fpl_token.do_refresh(force=True):
            sys.exit("Could not exchange that token. See the message above.")

    # ---- verify
    head("STEP 2 — VERIFY")
    try:
        entry, player = fpl_write.whoami()
    except Exception as e:
        sys.exit(f"Verification failed: {e}")

    name = f"{player.get('first_name')} {player.get('last_name')}"
    print(f"logged in as ....... {name}")
    print(f"access token ....... valid {fpl_write.token_hours_left():.1f}h")

    if not entry:
        print("team ............... none yet")
        head("NEXT")
        print("You have not created a team. Either make one on the FPL site, or:")
        print("  python fpl_create.py --name \"Your Team\" --club NONE")
        print("  python fpl_create.py --name \"Your Team\" --club NONE --confirm")
        return

    STATE.mkdir(exist_ok=True)
    cfg_path = STATE / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg["team_id"] = entry
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"team id ............ {entry}  (saved)")

    try:
        team = fpl_write.my_team(entry)
        tr = team["transfers"]
        free = "unlimited" if tr["limit"] is None else tr["limit"] - tr["made"]
        print(f"squad value ........ £{tr['value']/10:.1f}m")
        print(f"bank ............... £{tr['bank']/10:.1f}m")
        print(f"free transfers ..... {free}")
    except Exception as e:
        print(f"! could not read your squad: {e}")

    head("YOU'RE SET — WHAT TO RUN")
    print("""Look before you leap:

  python fpl_model.py --squad        score your current 15
  python fpl_optimise.py             what the model would pick instead
  python fpl_daily.py --auto         what it WOULD do, changing nothing

When you're happy:

  python fpl_daily.py --apply        do it once, now
  python run_scheduler.py            run it forever (08:00 / 20:00 / 23:30)

Or with Docker:

  docker compose up -d

Stop it touching your team at any time:

  touch state/PAUSE                  alerts continue, writes stop

Everything it writes is logged in state/writes.log with the exact payload.""")


if __name__ == "__main__":
    main()
