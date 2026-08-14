#!/usr/bin/env python3
"""
Scheduler. Runs the daily check at fixed local times, forever.

This replaces OS-level cron / Task Scheduler so the same setup works on Linux,
macOS, Windows and inside Docker without three different configurations.

    python run_scheduler.py                    # default: 08:00, 20:00, 23:30
    python run_scheduler.py --at 07:30 --at 18:00
    python run_scheduler.py --once             # run once and exit (for real cron)
    python run_scheduler.py --dry-run          # never writes to your team

Each run: refresh the access token if it is close to expiry, then run the daily
check. Both steps are logged to state/run.log.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
DEFAULT_TIMES = ["08:00", "20:00", "23:30"]


def log(msg):
    STATE.mkdir(exist_ok=True)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with (STATE / "run.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(script, args):
    cmd = [sys.executable, str(ROOT / script), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           encoding="utf-8", errors="replace")
        if r.stdout:
            for ln in r.stdout.rstrip().splitlines():
                log(f"  {ln}")
        if r.returncode != 0 and r.stderr:
            log(f"  ! {script} exited {r.returncode}: {r.stderr.strip()[:300]}")
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"  ! {script} timed out after 600s")
        return False


def cycle(dry_run=False):
    log("--- run start ---")
    run("fpl_token.py", ["--refresh"])
    run("fpl_daily.py", ["--auto"] if dry_run else ["--apply"])
    log("--- run end ---")


def next_fire(times):
    now = datetime.now()
    best = None
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        if best is None or when < best:
            best = when
    return best


def main():
    ap = argparse.ArgumentParser(description="FPL scheduler")
    ap.add_argument("--at", action="append", metavar="HH:MM",
                    help="run time, repeatable (default 08:00 20:00 23:30)")
    ap.add_argument("--once", action="store_true", help="run once and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, never write to your team")
    args = ap.parse_args()

    times = args.at or DEFAULT_TIMES
    for t in times:
        try:
            hh, mm = (int(x) for x in t.split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except (ValueError, AssertionError):
            sys.exit(f"bad --at value '{t}', expected HH:MM")

    if args.once:
        cycle(args.dry_run)
        return

    log(f"scheduler started; firing at {', '.join(times)} local"
        + ("  [DRY RUN]" if args.dry_run else ""))
    while True:
        nxt = next_fire(times)
        wait = (nxt - datetime.now()).total_seconds()
        log(f"next run {nxt.strftime('%Y-%m-%d %H:%M')} "
            f"(in {wait/3600:.1f}h)")
        time.sleep(max(1, wait))
        try:
            cycle(args.dry_run)
        except Exception as e:                      # never let one bad run kill the loop
            log(f"! run failed: {type(e).__name__}: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()
