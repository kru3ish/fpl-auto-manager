#!/usr/bin/env python3
"""
Off-machine watchdog. Runs in GitHub Actions, watches the team, needs no secrets.

Every other check in this repo runs on the same laptop as the engine it checks,
so when that laptop slept for nine days the outage was invisible: the thing that
reports health was part of the thing that stopped. A watchdog has to live
somewhere the failure cannot reach.

WHAT IT WATCHES, AND WHY NOT A HEARTBEAT. It does not ask "did the laptop run" --
that needs the laptop to report in, which is the same single point of failure
wearing a hat. It asks "is this team in a bad state with a deadline coming",
which is the thing that actually costs points. A dead engine shows up here
anyway: flags pile up in the XI and transfers stop being made.

NO CREDENTIALS. Every endpoint used is public -- entry/, history/, picks/ and
bootstrap-static all answer an anonymous GET. The only token involved is the
GITHUB_TOKEN that Actions injects for itself, which is why nothing from the FPL
side ever has to be stored in this repo.

WHAT IT CANNOT SEE. Picks for a gameweek are private until that deadline passes,
so the newest XI available is the one from the last deadline. That is still the
squad that carries into the next one unless somebody changes it, which is
exactly the thing being checked.

    python watchdog.py            # print findings
    python watchdog.py --json     # machine-readable, for the workflow
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import urllib.error
import urllib.request

API = "https://fantasy.premierleague.com/api/"
UA = {"User-Agent": "Mozilla/5.0 (fpl-auto-manager watchdog)"}

ENTRY = 4330346

# Only speak up inside this window. Outside it there is nothing the owner could
# usefully do yet, and an alert a week early is an alert that gets muted.
WARN_WITHIN_HOURS = 60.0

# Gameweeks with no transfer at all before an idle engine becomes suspicious.
# Banking transfers is a real strategy, so this has to be loose enough not to
# cry wolf over deliberate patience.
IDLE_GW_LIMIT = 3


def get(path, tries=3):
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(API + path, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
    raise RuntimeError(f"{path}: {last}")


def next_deadline(boot):
    for e in boot["events"]:
        if e.get("is_next"):
            return e["id"], datetime.datetime.fromisoformat(
                e["deadline_time"].replace("Z", "+00:00"))
    # End of season, or between the last fixture and the next event being named.
    for e in boot["events"]:
        if not e.get("finished"):
            return e["id"], datetime.datetime.fromisoformat(
                e["deadline_time"].replace("Z", "+00:00"))
    return None, None


def latest_picks(entry_id, current_gw):
    """
    The newest XI that is actually published.

    Walks backwards because a gameweek's picks appear only once its deadline has
    passed, and the current event can flip over before picks are served.
    """
    for gw in range(current_gw or 0, 0, -1):
        try:
            return gw, get(f"entry/{entry_id}/event/{gw}/picks/")
        except RuntimeError:
            continue
    return None, None


def check():
    boot = get("bootstrap-static/")
    entry = get(f"entry/{ENTRY}/")
    gw, deadline = next_deadline(boot)
    now = datetime.datetime.now(datetime.timezone.utc)
    hours = (deadline - now).total_seconds() / 3600 if deadline else None

    el = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}

    picks_gw, picks = latest_picks(ENTRY, entry.get("current_event"))
    findings, squad_note = [], None

    if picks:
        xi = [p for p in picks["picks"] if p["position"] <= 11]
        bench = [p for p in picks["picks"] if p["position"] > 11]
        healthy_bench = [p for p in bench
                         if el.get(p["element"], {}).get("status") == "a"]

        for p in xi:
            e = el.get(p["element"])
            if not e or e["status"] == "a":
                continue
            cop = e.get("chance_of_playing_next_round")
            # status: d=doubtful i=injured s=suspended u=unavailable n=not in squad
            hard = e["status"] in {"i", "s", "u", "n"} or cop == 0
            findings.append({
                "severity": "critical" if hard else "warning",
                "player": e["web_name"],
                "club": teams.get(e["team"], "?"),
                "detail": (f"status={e['status']}"
                           + (f" chance={cop}%" if cop is not None else "")
                           + (f" — {e['news']}" if e.get("news") else "")),
                "why": ("in the XI and will not feature" if hard
                        else "in the XI carrying a doubt"),
            })

        squad_note = (f"XI from GW{picks_gw} (newest published), "
                      f"{len(healthy_bench)} fit bench option(s)")

        if not healthy_bench and any(f["severity"] == "critical" for f in findings):
            findings.append({
                "severity": "critical", "player": "(bench)", "club": "-",
                "detail": "no available bench player",
                "why": "auto-subs cannot rescue a non-starter",
            })

    # An engine that has stopped looks like a squad nobody has touched. Transfers
    # are the visible trace of it being alive.
    hist = get(f"entry/{ENTRY}/history/").get("current", [])
    recent = hist[-IDLE_GW_LIMIT:]
    if len(recent) == IDLE_GW_LIMIT and all(
            (g.get("event_transfers") or 0) == 0 for g in recent):
        if any(f["severity"] in {"critical", "warning"} for f in findings):
            findings.append({
                "severity": "warning", "player": "(engine)", "club": "-",
                "detail": f"no transfers in {IDLE_GW_LIMIT} gameweeks",
                "why": "flags unaddressed and nothing moving — engine may be down",
            })

    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "team": entry.get("name"),
        "overall_rank": entry.get("summary_overall_rank"),
        "points": entry.get("summary_overall_points"),
        "next_gw": gw,
        "deadline": deadline.isoformat() if deadline else None,
        "hours_to_deadline": round(hours, 1) if hours is not None else None,
        "in_window": bool(hours is not None and 0 < hours <= WARN_WITHIN_HOURS),
        "squad_note": squad_note,
        "findings": findings,
        # Only worth waking a human for when there is both a problem and time
        # left to act on it.
        "alert": bool(findings
                      and hours is not None and 0 < hours <= WARN_WITHIN_HOURS),
    }


def render(r):
    lines = [
        f"**{r['team']}** — {r['points']} pts, OR {r['overall_rank']:,}"
        if r.get("overall_rank") else f"**{r['team']}**",
        "",
        f"GW{r['next_gw']} deadline: `{r['deadline']}` "
        f"({r['hours_to_deadline']}h away)",
    ]
    if r.get("squad_note"):
        lines.append(f"Squad checked: {r['squad_note']}")
    lines.append("")
    if not r["findings"]:
        lines.append("No problems found in the published XI.")
    else:
        lines.append("| | Player | Club | Why | Detail |")
        lines.append("|---|---|---|---|---|")
        for f in r["findings"]:
            icon = "🔴" if f["severity"] == "critical" else "🟡"
            lines.append(f"| {icon} | {f['player']} | {f['club']} | "
                         f"{f['why']} | {f['detail']} |")
        lines += [
            "",
            "The local engine should have handled this. If you did not get a "
            "briefing email, the machine running it is asleep or off — reply "
            "`STATUS` to a past briefing once it is awake, or fix the lineup "
            "on the FPL site directly.",
        ]
    lines += ["", f"<sub>watchdog ran {r['checked_at']} · public FPL API, no "
                  f"credentials · independent of the machine running the engine</sub>"]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="off-machine FPL watchdog")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        result = check()
    except Exception as exc:
        # A broken watchdog must be loud, or it becomes another thing that fails
        # quietly -- which is the entire problem it exists to solve.
        print(f"watchdog failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result))
        print(f"\nalert={result['alert']}")
