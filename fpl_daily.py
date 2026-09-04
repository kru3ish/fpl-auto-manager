#!/usr/bin/env python3
"""
FPL 2026/27 daily watchdog.

Run this once a day. It diffs today's FPL data against yesterday's snapshot and
tells you what changed about YOUR squad and what to do about it.

    python fpl_daily.py                      # full daily check
    python fpl_daily.py --team 1234567       # bind to your FPL team id
    python fpl_daily.py --live               # during matches: live pts + auto-subs
    python fpl_daily.py --horizon 6          # fixture window for rotation checks

First run takes a snapshot and reports no diffs. Every run after that compares.

Only dependency:  pip install requests
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://fantasy.premierleague.com/api"
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
REPORTS = ROOT / "reports"
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
CACHE_MINUTES = 60

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (fpl-daily)"})

# --------------------------------------------------------------- european load
# FPL's API has no European fixtures. These are the 2026/27 UEFA league-phase
# windows plus the English entrants, so we can flag midweek turnarounds.
EURO_CLUBS = {
    "ARS": "UCL", "MCI": "UCL", "MUN": "UCL", "AVL": "UCL", "LIV": "UCL",
    "CRY": "UEL", "BOU": "UEL", "SUN": "UEL",
}
EURO_MATCHDAYS = {
    "UCL": [
        ("MD1", "2026-09-08", "2026-09-10"), ("MD2", "2026-10-13", "2026-10-14"),
        ("MD3", "2026-10-20", "2026-10-21"), ("MD4", "2026-11-03", "2026-11-04"),
        ("MD5", "2026-11-24", "2026-11-25"), ("MD6", "2026-12-08", "2026-12-09"),
        ("MD7", "2027-01-19", "2027-01-20"), ("MD8", "2027-01-27", "2027-01-27"),
    ],
    "UEL": [
        ("MD1", "2026-09-16", "2026-09-17"), ("MD2", "2026-10-15", "2026-10-15"),
        ("MD3", "2026-10-22", "2026-10-22"), ("MD4", "2026-11-05", "2026-11-05"),
        ("MD5", "2026-11-26", "2026-11-26"), ("MD6", "2026-12-10", "2026-12-10"),
        ("MD7", "2027-01-21", "2027-01-21"), ("MD8", "2027-01-28", "2027-01-28"),
    ],
}

RED, AMBER, GREEN, INFO = "RED", "AMBER", "GREEN", "INFO"
SEV_ORDER = {RED: 0, AMBER: 1, GREEN: 2, INFO: 3}


# --------------------------------------------------------------------- plumbing
def get(path):
    r = SESSION.get(f"{BASE}/{path}", timeout=25)
    r.raise_for_status()
    return r.json()


def now_utc():
    return datetime.now(timezone.utc)


def cached(name, path, minutes=CACHE_MINUTES):
    STATE.mkdir(exist_ok=True)
    f = STATE / f"cache_{name}.json"
    if f.exists():
        age = now_utc() - datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        if age < timedelta(minutes=minutes):
            return json.loads(f.read_text(encoding="utf-8"))
    data = get(path)
    f.write_text(json.dumps(data), encoding="utf-8")
    return data


def load_state(name, default):
    f = STATE / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return default


def save_state(name, data):
    STATE.mkdir(exist_ok=True)
    (STATE / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").lower()


# ----------------------------------------------------------------- squad lookup
def resolve_squad(elements, teams_by_id):
    """Map the names in squad_and_fixtures.json to live FPL element ids, once."""
    cache = load_state("squad_ids", {})
    src = ROOT / "squad_and_fixtures.json"
    if not src.exists():
        return []

    plan = json.loads(src.read_text(encoding="utf-8"))
    wanted = plan.get("squad", {}).get("players", [])
    resolved, unresolved = [], []

    for entry in wanted:
        name, club = entry["name"], entry["club"]
        key = f"{name}|{club}"
        if key in cache:
            resolved.append((cache[key], entry))
            continue

        target = strip_accents(name)
        surname = target.split()[-1]
        pool = [p for p in elements if teams_by_id[p["team"]]["short_name"] == club]
        hit = None
        for p in pool:
            full = strip_accents(f"{p['first_name']} {p['second_name']}")
            web = strip_accents(p["web_name"])
            if full == target or web == target:
                hit = p
                break
        if not hit:
            for p in pool:
                full = strip_accents(f"{p['first_name']} {p['second_name']}")
                web = strip_accents(p["web_name"])
                if surname in full.split() or surname in web:
                    hit = p
                    break
        if hit:
            cache[key] = hit["id"]
            resolved.append((hit["id"], entry))
        else:
            unresolved.append(key)

    save_state("squad_ids", cache)
    if unresolved:
        print(f"  ! could not resolve: {', '.join(unresolved)}")
    return resolved


def squad_from_api(team_id, gw, elements_by_id):
    """Once you've entered a team, read the real picks instead of the plan file."""
    try:
        picks = get(f"entry/{team_id}/event/{max(1, gw - 1)}/picks/")
    except requests.HTTPError:
        # Before your first gameweek is played that endpoint 404s, but the
        # authenticated my-team endpoint already has the live squad. Without
        # this the report silently falls back to the PLANNED squad and shows
        # players you no longer own.
        try:
            import fpl_write
            s = fpl_write.session()
            team = fpl_write.my_team(team_id, s)
            out = []
            for pk in sorted(team["picks"], key=lambda x: x["position"]):
                p = elements_by_id[pk["element"]]
                out.append((pk["element"], {
                    "name": p["web_name"],
                    "start": pk["position"] <= 11,
                    "bench_order": pk["position"] - 12 if pk["position"] > 11 else None,
                    "is_captain": pk["is_captain"],
                    "is_vice": pk["is_vice_captain"],
                    "slot": pk["position"],
                }))
            return out, team
        except Exception:
            return None
    out = []
    for pk in picks["picks"]:
        p = elements_by_id[pk["element"]]
        out.append((pk["element"], {
            "name": p["web_name"],
            "start": pk["position"] <= 11,
            "bench_order": pk["position"] - 12 if pk["position"] > 11 else None,
            "is_captain": pk["is_captain"],
            "is_vice": pk["is_vice_captain"],
            "slot": pk["position"],
        }))
    return out, picks


# ------------------------------------------------------------------ euro checks
def euro_conflicts(club, deadline, window_days=5):
    """Any European matchday landing within `window_days` before the deadline."""
    comp = EURO_CLUBS.get(club)
    if not comp:
        return []
    hits = []
    for label, start, end in EURO_MATCHDAYS[comp]:
        s = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        e = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)
        gap = (deadline - e).days
        if 0 <= gap <= window_days:
            hits.append((comp, label, end, max(0, gap)))
    return hits


# ------------------------------------------------------------------- the checks
def check_flags(squad, elements_by_id, teams_by_id, prev, alerts):
    """New injuries, cleared injuries, changed news for anyone in your squad."""
    snapshot = {}
    for pid, entry in squad:
        p = elements_by_id[pid]
        cop = p["chance_of_playing_next_round"]
        news = (p["news"] or "").strip()
        snapshot[str(pid)] = {"cop": cop, "news": news, "cost": p["now_cost"],
                              "own": p["selected_by_percent"]}

        old = prev.get(str(pid))
        starter = entry.get("start", False)
        label = f"{p['web_name']} ({teams_by_id[p['team']]['short_name']})"
        role = "XI" if starter else "bench"

        if old is None:
            if cop not in (None, 100):
                alerts.append((RED if starter else AMBER, "FITNESS",
                               f"{label} is flagged at {cop}% — {news or 'no detail'} [{role}]"))
            continue

        if old["news"] != news or old["cop"] != cop:
            if cop in (None, 100) and old["cop"] not in (None, 100):
                alerts.append((GREEN, "FITNESS", f"{label} is CLEARED (was {old['cop']}%)"))
            elif cop == 0:
                alerts.append((RED, "FITNESS",
                               f"{label} is RULED OUT — {news}. Replace or bench now. [{role}]"))
            elif cop is not None and (old["cop"] is None or cop < old["cop"]):
                sev = RED if (starter and cop <= 50) else AMBER
                alerts.append((sev, "FITNESS",
                               f"{label} downgraded to {cop}% — {news} [{role}]"))
            else:
                alerts.append((AMBER, "FITNESS", f"{label} news changed: {news or 'cleared'} ({cop}%)"))
    return snapshot


def check_prices(squad, watchlist, elements_by_id, teams_by_id, prev, alerts):
    """Actual price moves since yesterday + tonight's likely movers."""
    for pid, entry in squad:
        p = elements_by_id[pid]
        old = prev.get(str(pid))
        if not old:
            continue
        if p["now_cost"] != old["cost"]:
            direction = "ROSE" if p["now_cost"] > old["cost"] else "FELL"
            sev = GREEN if direction == "ROSE" else AMBER
            alerts.append((sev, "PRICE",
                           f"{p['web_name']} {direction} to £{p['now_cost']/10:.1f}m "
                           f"(was £{old['cost']/10:.1f}m)"))

    # momentum: net transfers this gameweek relative to how many own the player
    movers = []
    for p in elements_by_id.values():
        net = p["transfers_in_event"] - p["transfers_out_event"]
        own = float(p["selected_by_percent"] or 0)
        if own < 0.3 or abs(net) < 15000:
            continue
        pressure = net / max(own, 0.3)
        movers.append((pressure, net, p))
    movers.sort(key=lambda r: -abs(r[0]))

    owned_ids = {pid for pid, _ in squad}
    for pressure, net, p in movers[:25]:
        club = teams_by_id[p["team"]]["short_name"]
        if p["id"] in owned_ids and net < 0:
            alerts.append((AMBER, "PRICE",
                           f"{p['web_name']} ({club}) is bleeding {net:,} net transfers — "
                           f"likely price FALL. You own him; sell before ~02:30 or eat it."))
        elif p["id"] in {w["id"] for w in watchlist} and net > 0:
            alerts.append((AMBER, "PRICE",
                           f"{p['web_name']} ({club}, £{p['now_cost']/10:.1f}m) is +{net:,} net — "
                           f"likely price RISE tonight. Buy now if you were going to."))


def check_captain(squad, elements_by_id, teams_by_id, deadline, fixtures_by_team, alerts):
    """The Haaland question: is your captain safe to captain?"""
    cap = next((pid for pid, e in squad if e.get("is_captain")), None)
    vice = next((pid for pid, e in squad if e.get("is_vice")), None)

    if cap is None:
        plan = json.loads((ROOT / "squad_and_fixtures.json").read_text(encoding="utf-8"))
        cap_name = plan.get("squad", {}).get("captain_gw1")
        vice_name = plan.get("squad", {}).get("vice_gw1")
        for pid, entry in squad:
            n = strip_accents(entry.get("name", ""))
            if cap_name and strip_accents(cap_name).split()[-1] in n:
                cap = pid
            if vice_name and strip_accents(vice_name).split()[-1] in n:
                vice = pid

    for pid, role in ((cap, "CAPTAIN"), (vice, "VICE")):
        if pid is None:
            alerts.append((AMBER, "CAPTAIN", f"No {role.lower()} identified — set one."))
            continue
        p = elements_by_id[pid]
        club = teams_by_id[p["team"]]["short_name"]
        cop = p["chance_of_playing_next_round"]

        if cop == 0:
            alerts.append((RED, role,
                           f"{p['web_name']} is OUT — {p['news']}. Move the armband."))
        elif cop is not None and cop < 100:
            sev = RED if cop <= 50 else AMBER
            alerts.append((sev, role,
                           f"{p['web_name']} at {cop}% — {p['news']}. "
                           f"{'Do not captain.' if cop <= 75 else 'Monitor to deadline.'}"))

        for comp, md, played, gap in euro_conflicts(club, deadline):
            sev = RED if gap <= 2 else AMBER
            alerts.append((sev, role,
                           f"{club} play {comp} {md} on {played} — only {gap} day(s) "
                           f"before this deadline. Rotation/fatigue risk on {p['web_name']}."))

        fx = fixtures_by_team.get(p["team"], [])
        if fx:
            gw_fx = fx[0]
            if gw_fx["difficulty"] >= 4:
                alerts.append((AMBER, role,
                               f"{p['web_name']} faces {gw_fx['opp']}{gw_fx['ha']} "
                               f"(FDR {gw_fx['difficulty']}) — hardest tier. Consider an alternative."))


def check_rotation(squad, elements_by_id, teams_by_id, deadline, alerts):
    """European load across the whole squad, not just the captain."""
    by_club = defaultdict(list)
    for pid, entry in squad:
        p = elements_by_id[pid]
        if entry.get("start", True):
            by_club[teams_by_id[p["team"]]["short_name"]].append(p["web_name"])

    for club, names in sorted(by_club.items()):
        for comp, md, played, gap in euro_conflicts(club, deadline):
            sev = RED if (gap <= 2 and len(names) >= 2) else AMBER
            alerts.append((sev, "ROTATION",
                           f"{club} ({', '.join(names)}) play {comp} {md} on {played}, "
                           f"{gap} day(s) before the deadline."))


def check_legal_xi(squad, elements_by_id, alerts):
    """Would your XI still be legal if every flagged player blanked?"""
    xi = [(pid, e) for pid, e in squad if e.get("start", True)]
    risky = []
    counts = defaultdict(int)
    for pid, e in xi:
        p = elements_by_id[pid]
        cop = p["chance_of_playing_next_round"]
        if cop is not None and cop <= 50:
            risky.append(p["web_name"])
        else:
            counts[POS[p["element_type"]]] += 1

    if not risky:
        return
    if counts["GK"] < 1 or counts["DEF"] < 3 or counts["FWD"] < 1:
        alerts.append((RED, "SHAPE",
                       f"If {', '.join(risky)} blank, your XI cannot auto-sub into a legal "
                       f"shape (need 1 GK / 3 DEF / 1 FWD). Fix the bench order or transfer."))
    else:
        alerts.append((AMBER, "SHAPE",
                       f"{', '.join(risky)} at risk, but auto-subs still cover you "
                       f"({counts['GK']} GK / {counts['DEF']} DEF / {counts['FWD']} FWD safe)."))


def check_auth(alerts):
    """
    Auth health, surfaced in the ACTION LIST rather than buried below.

    The write half silently sat dead for six days once because a broken token
    chain only ever printed "AUTO OFF" in a lower section nobody reads. A dead
    credential is an incident, not a footnote.
    """
    state_file = STATE / "auth_failures.json"
    hist = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    try:
        import fpl_token
        import fpl_write
    except ImportError:
        return

    stored = fpl_token._read().get("refresh_token")
    if not stored:
        alerts.append((RED, "AUTH", "No refresh token stored — the engine cannot "
                                    "touch your team. Run: python fpl_token.py --login"))
        return

    hours = fpl_write.token_hours_left()
    if hours > 0.5:
        if hist.get("consecutive"):
            alerts.append((GREEN, "AUTH", "Token chain recovered."))
        state_file.write_text(json.dumps({"consecutive": 0}), encoding="utf-8")
        return

    # access token is dead — can the refresh chain still mint a new one?
    try:
        fpl_token.exchange(stored)
        state_file.write_text(json.dumps({"consecutive": 0}), encoding="utf-8")
        return
    except Exception as e:
        n = hist.get("consecutive", 0) + 1
        state_file.write_text(json.dumps({"consecutive": n}), encoding="utf-8")
        days = n / 3.0                       # the scheduler fires three times a day
        alerts.append((RED, "AUTH",
                       f"**TOKEN CHAIN BROKEN** — {n} consecutive failures "
                       f"(~{days:.1f} days). Your team is NOT being managed. "
                       f"Cause: {str(e)[:90]}. "
                       f"Fix: python fpl_token.py --login"))


def check_deadline(deadline, alerts):
    left = deadline - now_utc()
    hours = left.total_seconds() / 3600
    if hours < 0:
        alerts.append((INFO, "DEADLINE", "Deadline has passed. Next gameweek is live."))
    elif hours <= 4:
        alerts.append((RED, "DEADLINE",
                       f"{hours:.1f}h to deadline. Final team check NOW — "
                       f"press conferences are already out."))
    elif hours <= 30:
        alerts.append((AMBER, "DEADLINE",
                       f"{hours:.0f}h to deadline. Press conferences land today/tomorrow. "
                       f"Don't transfer before you've read them."))
    else:
        alerts.append((INFO, "DEADLINE", f"{left.days}d {left.seconds//3600}h to deadline."))


# --------------------------------------------------------------------- live mode
def run_live(team_id, gw, elements_by_id, teams_by_id):
    picks = get(f"entry/{team_id}/event/{gw}/picks/")
    live = {e["id"]: e for e in get(f"event/{gw}/live/")["elements"]}
    fixtures = get(f"fixtures/?event={gw}")
    started = {f["id"]: f["started"] for f in fixtures}
    finished = {f["id"]: f["finished_provisional"] for f in fixtures}

    print(f"\n=== GW{gw} LIVE ===  chip: {picks.get('active_chip') or 'none'}")
    print(f"{'':2}{'PLAYER':<16}{'POS':<5}{'CLUB':<6}{'MIN':>4}{'G':>3}{'A':>3}"
          f"{'CS':>3}{'DC':>4}{'BPS':>5}{'BON':>4}{'PTS':>5}")
    print("-" * 62)

    rows = []
    for pk in picks["picks"]:
        p = elements_by_id[pk["element"]]
        st = live.get(pk["element"], {}).get("stats", {})
        expl = live.get(pk["element"], {}).get("explain", [])
        fids = [e["fixture"] for e in expl]
        rows.append({
            "name": p["web_name"], "pos": POS[p["element_type"]],
            "club": teams_by_id[p["team"]]["short_name"], "slot": pk["position"],
            "mult": pk["multiplier"], "mins": st.get("minutes", 0),
            "pts": st.get("total_points", 0) * pk["multiplier"],
            "bps": st.get("bps", 0), "bonus": st.get("bonus", 0),
            "g": st.get("goals_scored", 0), "a": st.get("assists", 0),
            "cs": st.get("clean_sheets", 0), "dc": st.get("defensive_contribution", 0),
            "kicked": any(started.get(f) for f in fids),
            "done": all(finished.get(f) for f in fids) if fids else False,
        })

    xi = [r for r in rows if r["slot"] <= 11]
    bench = sorted((r for r in rows if r["slot"] > 11), key=lambda r: r["slot"])
    for r in xi:
        tag = "(C)" if r["mult"] == 2 else "(TC)" if r["mult"] == 3 else ""
        mark = " " if r["done"] else ("*" if r["kicked"] else ".")
        print(f"{mark:2}{r['name']+tag:<16}{r['pos']:<5}{r['club']:<6}{r['mins']:>4}"
              f"{r['g']:>3}{r['a']:>3}{r['cs']:>3}{r['dc']:>4}{r['bps']:>5}"
              f"{r['bonus']:>4}{r['pts']:>5}")
    print("-" * 62)
    print("BENCH:", "  ".join(f"{r['name']} ({r['pts']})" for r in bench))
    print(f"\nXI live total: {sum(r['pts'] for r in xi)}   "
          f"FPL says: {picks['entry_history'].get('points')}   "
          f"rank: {picks['entry_history'].get('overall_rank')}")

    blanks = [r for r in xi if r["done"] and r["mins"] == 0]
    if picks.get("active_chip") == "bboost":
        print("\nBench Boost active — no auto-subs.")
    elif blanks:
        print("\n--- PROJECTED AUTO-SUBS ---")
        avail = [b for b in bench if b["mins"] > 0 or not b["done"]]
        counts = defaultdict(int)
        for r in xi:
            if r["mins"] > 0 or not r["done"]:
                counts[r["pos"]] += 1
        for out in blanks:
            if out["pos"] == "GK":
                gk = next((b for b in avail if b["pos"] == "GK"), None)
                if gk:
                    print(f"  {out['name']} -> {gk['name']}")
                    avail.remove(gk)
                continue
            for b in [x for x in avail if x["pos"] != "GK"]:
                trial = dict(counts)
                trial[b["pos"]] += 1
                if trial["DEF"] >= 3 and trial["FWD"] >= 1:
                    print(f"  {out['name']} -> {b['name']}")
                    avail.remove(b)
                    counts = trial
                    break
            else:
                print(f"  {out['name']} -> no legal replacement")
    else:
        pending = [r for r in xi if not r["done"]]
        print(f"\n{len(pending)} player(s) still to play." if pending
              else "\nNo auto-subs needed.")
    print("\nnote: bonus stays provisional until ~1h after the last whistle.")


# ------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="FPL 2026/27 daily watchdog")
    ap.add_argument("--team", type=int, help="your FPL entry id (saved after first use)")
    ap.add_argument("--live", action="store_true", help="live points + auto-subs")
    ap.add_argument("--gw", type=int)
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--no-save", action="store_true", help="don't update the snapshot")
    ap.add_argument("--auto", action="store_true",
                    help="run the auto-action engine (dry run unless --apply)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write changes to your team. Implies --auto.")
    args = ap.parse_args()
    if args.apply:
        args.auto = True

    # An owner HOLD has to actually stop writes. Reporting the freeze in the
    # briefing while the engine kept transferring would be worse than having no
    # override at all -- the analysis still runs, only the writing stops.
    if args.apply:
        try:
            import fpl_inbox
            if fpl_inbox.is_paused():
                print("OWNER HOLD in effect - analysing only, no writes. "
                      "Reply RESUME to re-enable.")
                args.apply = False
        except Exception as exc:
            print(f"could not check owner hold ({exc}); continuing")

    cfg = load_state("config", {})
    if args.team:
        cfg["team_id"] = args.team
        save_state("config", cfg)
    team_id = cfg.get("team_id")

    boot = cached("bootstrap", "bootstrap-static/")
    elements_by_id = {p["id"]: p for p in boot["elements"]}
    teams_by_id = {t["id"]: t for t in boot["teams"]}

    events = boot["events"]
    nxt = next((e for e in events if e.get("is_next")), None)
    cur = next((e for e in events if e.get("is_current")), None)
    gw = args.gw or (nxt or cur or events[0])["id"]
    ev = next(e for e in events if e["id"] == gw)
    deadline = datetime.fromisoformat(ev["deadline_time"].replace("Z", "+00:00"))

    if args.live:
        if not team_id:
            sys.exit("Live mode needs a team id: python fpl_daily.py --team 1234567 --live")
        run_live(team_id, (cur or ev)["id"], elements_by_id, teams_by_id)
        return

    # ---- squad
    api_picks = squad_from_api(team_id, gw, elements_by_id) if team_id else None
    if api_picks:
        squad, _ = api_picks
        source = f"live team {team_id}"
    else:
        squad = resolve_squad(boot["elements"], teams_by_id)
        source = "squad_and_fixtures.json (planned squad)"
    if not squad:
        sys.exit(
            "No squad found.\n\n"
            "Either:\n"
            "  1. python fpl_token.py --set-refresh   (then this reads your live team)\n"
            "  2. python fpl_daily.py --team 1234567  (public data only, no auth)\n\n"
            "Your team id is the number in the URL when you view your own team:\n"
            "  fantasy.premierleague.com/entry/1234567/event/1")

    # ---- upcoming fixtures per club
    all_fx = cached("fixtures", "fixtures/", minutes=180)
    fixtures_by_team = defaultdict(list)
    for f in sorted((x for x in all_fx if x["event"] and gw <= x["event"] < gw + args.horizon),
                    key=lambda x: x["event"]):
        fixtures_by_team[f["team_h"]].append(
            {"gw": f["event"], "opp": teams_by_id[f["team_a"]]["short_name"],
             "ha": "(H)", "difficulty": f["team_h_difficulty"]})
        fixtures_by_team[f["team_a"]].append(
            {"gw": f["event"], "opp": teams_by_id[f["team_h"]]["short_name"],
             "ha": "(A)", "difficulty": f["team_a_difficulty"]})

    # ---- watchlist = named upgrade targets worth price-watching
    watch_names = ["Gabriel", "Semenyo", "Isak", "Saka", "Thiago", "Rogers",
                   "Wirtz", "Porro", "Mateta", "Ndiaye"]
    watchlist = [p for p in boot["elements"] if p["web_name"] in watch_names]

    prev = load_state("snapshot", {})
    first_run = not prev
    alerts = []

    check_deadline(deadline, alerts)
    check_auth(alerts)
    snapshot = check_flags(squad, elements_by_id, teams_by_id, prev, alerts)
    if not first_run:
        check_prices(squad, watchlist, elements_by_id, teams_by_id, prev, alerts)
    check_captain(squad, elements_by_id, teams_by_id, deadline, fixtures_by_team, alerts)
    check_rotation(squad, elements_by_id, teams_by_id, deadline, alerts)
    check_legal_xi(squad, elements_by_id, alerts)

    # ---- report
    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"# FPL daily check — {stamp}")
    lines.append("")
    lines.append(f"- Squad source: {source}")
    lines.append(f"- Next deadline: GW{gw}, {deadline.strftime('%a %d %b %Y %H:%M UTC')}")
    lines.append("")

    alerts.sort(key=lambda a: SEV_ORDER[a[0]])
    lines.append("## Action list")
    lines.append("")
    if first_run:
        lines.append("_First run — snapshot taken. Tomorrow's run will show what changed._")
        lines.append("")
    actionable = [a for a in alerts if a[0] in (RED, AMBER)]
    if not actionable:
        lines.append("Nothing to act on. Squad is clean, no price pressure, captain is safe.")
    for sev, kind, msg in alerts:
        lines.append(f"- **[{sev}] {kind}** — {msg}")
    lines.append("")

    if args.auto:
        import fpl_auto
        lines.append("## Auto actions")
        lines.append("")
        for ln in fpl_auto.run(elements_by_id, teams_by_id, fixtures_by_team,
                               gw, deadline, apply=args.apply):
            lines.append(f"- {ln}")
        lines.append("")

    lines.append("## Squad status")
    lines.append("")
    lines.append("| Player | Pos | Club | £ | Own% | Fit | Next fixtures |")
    lines.append("|---|---|---|---|---|---|---|")
    for pid, entry in squad:
        p = elements_by_id[pid]
        club = teams_by_id[p["team"]]["short_name"]
        cop = p["chance_of_playing_next_round"]
        fit = "OK" if cop in (None, 100) else f"{cop}%"
        run = " ".join(f"{f['opp']}{f['ha']}{f['difficulty']}"
                       for f in fixtures_by_team.get(p["team"], [])[:4])
        star = "" if entry.get("start", True) else " (bench)"
        lines.append(f"| {p['web_name']}{star} | {POS[p['element_type']]} | {club} | "
                     f"{p['now_cost']/10:.1f} | {p['selected_by_percent']} | {fit} | {run} |")
    lines.append("")

    report = "\n".join(lines)
    print()
    print(report)

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{now_utc().strftime('%Y-%m-%d')}.md"
    out.write_text(report, encoding="utf-8")
    print(f"\nsaved: {out}")

    if not args.no_save:
        save_state("snapshot", snapshot)


if __name__ == "__main__":
    main()
