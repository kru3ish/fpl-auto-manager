#!/usr/bin/env python3
"""
FPL 2026/27 tracker + advisor.

Four things it does:
  1. live      - live points for your XI, with projected bonus and auto-subs
  2. subs      - who to start / bench for the upcoming deadline
  3. transfers - ranked transfer targets by position, fixture-weighted
  4. chips     - fixture-swing report to time your Wildcard / TC / BB

Usage:
    python fpl_tracker.py live      --team 1234567
    python fpl_tracker.py subs      --team 1234567
    python fpl_tracker.py transfers --team 1234567 --pos MID --budget 8.5
    python fpl_tracker.py chips     --horizon 6

Only dependency is `requests`:  pip install requests
No auth needed for any of this - all endpoints below are public.
"""

import argparse
import json
import sys
from collections import defaultdict

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

BASE = "https://fantasy.premierleague.com/api"

# ---------------------------------------------------------------- endpoints
# bootstrap-static/            every player, price, ownership, team, gameweek
# fixtures/?event=N            fixtures + official FDR for one gameweek
# element-summary/{id}/        one player's full history + upcoming fixtures
# entry/{team_id}/             your manager profile
# entry/{team_id}/history/     your season + past seasons
# entry/{team_id}/event/{gw}/picks/   your XI, bench order, captain, chip
# event/{gw}/live/             live stats + points for every player
# leagues-classic/{id}/standings/     mini-league table
# ---------------------------------------------------------------------------

POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (fpl-tracker)"})


def get(path):
    r = SESSION.get(f"{BASE}/{path}", timeout=20)
    r.raise_for_status()
    return r.json()


def bootstrap():
    data = get("bootstrap-static/")
    players = {p["id"]: p for p in data["elements"]}
    teams = {t["id"]: t for t in data["teams"]}
    events = data["events"]
    return players, teams, events


def current_gw(events):
    for e in events:
        if e["is_current"]:
            return e["id"]
    for e in events:
        if e["is_next"]:
            return e["id"]
    return 1


def next_gw(events):
    for e in events:
        if e["is_next"]:
            return e["id"]
    return current_gw(events)


# ------------------------------------------------------------------- 1. live
def cmd_live(args):
    players, teams, events = bootstrap()
    gw = args.gw or current_gw(events)

    picks = get(f"entry/{args.team}/event/{gw}/picks/")
    live = get(f"event/{gw}/live/")
    live_by_id = {e["id"]: e for e in live["elements"]}
    fixtures = get(f"fixtures/?event={gw}")
    started = {f["id"]: f["started"] for f in fixtures}
    finished = {f["id"]: f["finished_provisional"] for f in fixtures}

    chip = picks.get("active_chip")
    entry = picks["entry_history"]

    rows = []
    for pick in picks["picks"]:
        pid = pick["element"]
        p = players[pid]
        lv = live_by_id.get(pid, {})
        stats = lv.get("stats", {})
        expl = lv.get("explain", [])

        fixture_ids = [e["fixture"] for e in expl] or []
        has_played = stats.get("minutes", 0) > 0
        kicked_off = any(started.get(f) for f in fixture_ids)
        done = all(finished.get(f) for f in fixture_ids) if fixture_ids else False

        raw = stats.get("total_points", 0)
        pts = raw * pick["multiplier"]

        rows.append({
            "pid": pid,
            "name": p["web_name"],
            "pos": POS[p["element_type"]],
            "club": teams[p["team"]]["short_name"],
            "slot": pick["position"],
            "mult": pick["multiplier"],
            "raw": raw,
            "pts": pts,
            "mins": stats.get("minutes", 0),
            "bonus": stats.get("bonus", 0),
            "bps": stats.get("bps", 0),
            "goals": stats.get("goals_scored", 0),
            "assists": stats.get("assists", 0),
            "cs": stats.get("clean_sheets", 0),
            "dc": stats.get("defensive_contribution", 0),
            "played": has_played,
            "kicked_off": kicked_off,
            "done": done,
        })

    xi = [r for r in rows if r["slot"] <= 11]
    bench = sorted([r for r in rows if r["slot"] > 11], key=lambda r: r["slot"])

    print(f"\n=== GW{gw} LIVE ===  chip: {chip or 'none'}")
    print(f"{'':2} {'PLAYER':<16}{'POS':<5}{'CLUB':<6}{'MIN':>4}{'G':>3}{'A':>3}"
          f"{'CS':>3}{'DC':>4}{'BPS':>5}{'BON':>4}{'PTS':>5}")
    print("-" * 62)
    for r in xi:
        cap = "(C)" if r["mult"] == 2 else "(TC)" if r["mult"] == 3 else ""
        flag = " " if r["done"] else ("*" if r["kicked_off"] else ".")
        print(f"{flag:2} {r['name'] + cap:<16}{r['pos']:<5}{r['club']:<6}"
              f"{r['mins']:>4}{r['goals']:>3}{r['assists']:>3}{r['cs']:>3}"
              f"{r['dc']:>4}{r['bps']:>5}{r['bonus']:>4}{r['pts']:>5}")
    print("-" * 62)
    print("BENCH:", "  ".join(f"{r['name']} ({r['pts']})" for r in bench))

    total = sum(r["pts"] for r in xi)
    print(f"\nXI total (live): {total}")
    print(f"FPL says: {entry.get('points')} pts | hits: -{entry.get('event_transfers_cost', 0)}")
    print(f"Overall rank: {entry.get('overall_rank')}")
    print("\nlegend: '.' not kicked off   '*' in progress   ' ' finished")
    print("note: bonus is provisional until ~1hr after the last whistle.")

    # ---- auto-sub projection
    if chip == "bboost":
        print("\nBench Boost active - no auto-subs.")
        return

    blanks = [r for r in xi if r["done"] and r["mins"] == 0]
    if not blanks:
        pending = [r for r in xi if not r["done"]]
        if pending:
            print(f"\n{len(pending)} player(s) still to play - auto-subs not settled yet.")
        else:
            print("\nNo auto-subs: everyone in the XI played.")
        return

    print("\n--- PROJECTED AUTO-SUBS ---")
    avail = [b for b in bench if b["mins"] > 0 or not b["done"]]
    gk_out = [b for b in blanks if b["pos"] == "GK"]
    outfield_out = [b for b in blanks if b["pos"] != "GK"]

    bench_gk = [b for b in bench if b["pos"] == "GK"]
    if gk_out and bench_gk:
        print(f"  {gk_out[0]['name']} (0 min)  ->  {bench_gk[0]['name']}")

    counts = defaultdict(int)
    for r in xi:
        if r["mins"] > 0 or not r["done"]:
            counts[r["pos"]] += 1

    for out in outfield_out:
        for b in [x for x in avail if x["pos"] != "GK"]:
            trial = dict(counts)
            trial[b["pos"]] += 1
            if trial["DEF"] >= 3 and trial["FWD"] >= 1:
                print(f"  {out['name']} (0 min)  ->  {b['name']}")
                avail.remove(b)
                counts = trial
                break
        else:
            print(f"  {out['name']} (0 min)  ->  no legal replacement")


# ------------------------------------------------------------------- 2. subs
def fixture_score(players, teams, pid, gw, horizon=1):
    """Lower is easier. Uses the official FDR from the fixtures endpoint."""
    summary = get(f"element-summary/{pid}/")
    up = [f for f in summary["fixtures"] if gw <= f["event"] < gw + horizon]
    if not up:
        return None, []
    diffs = [f["difficulty"] for f in up]
    labels = []
    for f in up:
        opp = teams[f["team_a"] if f["is_home"] else f["team_h"]]["short_name"]
        labels.append(f"{opp}{'(H)' if f['is_home'] else '(A)'}[{f['difficulty']}]")
    return sum(diffs) / len(diffs), labels


def cmd_subs(args):
    players, teams, events = bootstrap()
    gw = args.gw or next_gw(events)
    prev = max(1, gw - 1)
    picks = get(f"entry/{args.team}/event/{prev}/picks/")

    print(f"\n=== START / BENCH FOR GW{gw} ===\n")
    rows = []
    for pick in picks["picks"]:
        p = players[pick["element"]]
        pid = p["id"]
        avg_fdr, labels = fixture_score(players, teams, pid, gw, 1)
        if avg_fdr is None:
            rows.append((p, None, ["BLANK"], -99))
            continue
        form = float(p["form"] or 0)
        ppg = float(p["points_per_game"] or 0)
        mins_risk = 0
        if p["chance_of_playing_next_round"] not in (None, 100):
            mins_risk = -3
        # simple score: form and history, penalised by fixture difficulty
        score = (form * 1.5) + ppg - (avg_fdr * 1.2) + mins_risk
        rows.append((p, avg_fdr, labels, score))

    rows.sort(key=lambda r: r[3], reverse=True)
    print(f"{'PLAYER':<18}{'POS':<5}{'FIXTURE':<18}{'FORM':>6}{'PPG':>6}{'SCORE':>7}  NOTE")
    print("-" * 80)
    for p, fdr, labels, score in rows:
        note = ""
        if p["chance_of_playing_next_round"] not in (None, 100):
            note = f"FLAG {p['chance_of_playing_next_round']}% - {(p['news'] or '')[:40]}"
        print(f"{p['web_name']:<18}{POS[p['element_type']]:<5}{labels[0]:<18}"
              f"{p['form']:>6}{p['points_per_game']:>6}{score:>7.1f}  {note}")

    print("\nPick the top 11 that still makes a legal shape (1 GK, 3+ DEF, 1+ FWD).")
    print("Anything with a FLAG under 75% goes to the bench regardless of score.")


# -------------------------------------------------------------- 3. transfers
def cmd_transfers(args):
    players, teams, events = bootstrap()
    gw = args.gw or next_gw(events)
    horizon = args.horizon

    want = None
    if args.pos:
        want = {v: k for k, v in POS.items()}[args.pos.upper()]

    owned = set()
    if args.team:
        try:
            picks = get(f"entry/{args.team}/event/{max(1, gw-1)}/picks/")
            owned = {p["element"] for p in picks["picks"]}
        except Exception:
            pass

    pool = []
    for p in players.values():
        if want and p["element_type"] != want:
            continue
        if args.budget and p["now_cost"] / 10 > args.budget:
            continue
        if p["minutes"] < args.min_minutes:
            continue
        if p["chance_of_playing_next_round"] not in (None, 100):
            continue
        pool.append(p)

    # shortlist on raw output first so we don't hammer element-summary
    pool.sort(key=lambda p: (float(p["form"] or 0) * 2 + float(p["points_per_game"] or 0)),
              reverse=True)
    pool = pool[:args.scan]

    scored = []
    for p in pool:
        avg_fdr, labels = fixture_score(players, teams, p["id"], gw, horizon)
        if avg_fdr is None:
            continue
        form = float(p["form"] or 0)
        ppg = float(p["points_per_game"] or 0)
        price = p["now_cost"] / 10
        value = ppg / price if price else 0
        score = (form * 2) + (ppg * 1.5) + (value * 3) - (avg_fdr * 2)
        scored.append((p, price, avg_fdr, labels, value, score))

    scored.sort(key=lambda r: r[5], reverse=True)

    print(f"\n=== TRANSFER TARGETS: GW{gw}-{gw+horizon-1}"
          f"{' | ' + args.pos.upper() if args.pos else ''}"
          f"{' | max ' + str(args.budget) + 'm' if args.budget else ''} ===\n")
    print(f"{'PLAYER':<18}{'POS':<5}{'CLUB':<6}{'£':>6}{'FORM':>6}{'PPG':>6}"
          f"{'OWN%':>7}{'FDR':>5}{'SCORE':>7}")
    print("-" * 72)
    for p, price, fdr, labels, value, score in scored[:args.top]:
        mark = "*" if p["id"] in owned else " "
        print(f"{mark}{p['web_name']:<17}{POS[p['element_type']]:<5}"
              f"{teams[p['team']]['short_name']:<6}{price:>6.1f}{p['form']:>6}"
              f"{p['points_per_game']:>6}{p['selected_by_percent']:>7}"
              f"{fdr:>5.1f}{score:>7.1f}")
        print(f"   run: {' '.join(labels)}")
    print("\n* = already in your squad")


# ------------------------------------------------------------------ 4. chips
def cmd_chips(args):
    players, teams, events = bootstrap()
    gw = args.gw or next_gw(events)
    horizon = args.horizon

    agg = defaultdict(list)
    for g in range(gw, gw + horizon):
        try:
            fixtures = get(f"fixtures/?event={g}")
        except Exception:
            break
        for f in fixtures:
            agg[f["team_h"]].append(("H", f["team_a"], f["team_h_difficulty"], g))
            agg[f["team_a"]].append(("A", f["team_h"], f["team_a_difficulty"], g))

    table = []
    for tid, runs in agg.items():
        avg = sum(r[2] for r in runs) / len(runs)
        greens = sum(1 for r in runs if r[2] <= 2)
        table.append((teams[tid]["short_name"], avg, greens, runs))
    table.sort(key=lambda r: r[1])

    print(f"\n=== FIXTURE SWING GW{gw}-{gw + horizon - 1} ===\n")
    print(f"{'TEAM':<6}{'AVG FDR':>9}{'EASY':>6}   RUN")
    print("-" * 72)
    for name, avg, greens, runs in table:
        run = " ".join(f"{teams[r[1]]['short_name']}{r[0]}{r[2]}" for r in runs)
        print(f"{name:<6}{avg:>9.2f}{greens:>6}   {run}")

    print("\nRule of thumb:")
    print("  avg FDR <= 2.5  -> load up, this is where your budget goes")
    print("  avg FDR >= 3.5  -> fade, even the good attackers")
    print("  Wildcard when 3+ of your squad's clubs flip from green to red at once.")
    print("  Triple Captain: a premium at HOME to a team with FDR 2 in this window.")


# -------------------------------------------------------------------- driver
def main():
    ap = argparse.ArgumentParser(description="FPL 2026/27 tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("live"); a.add_argument("--team", type=int, required=True)
    a.add_argument("--gw", type=int); a.set_defaults(fn=cmd_live)

    b = sub.add_parser("subs"); b.add_argument("--team", type=int, required=True)
    b.add_argument("--gw", type=int); b.set_defaults(fn=cmd_subs)

    c = sub.add_parser("transfers")
    c.add_argument("--team", type=int)
    c.add_argument("--pos", choices=["GK", "DEF", "MID", "FWD",
                                     "gk", "def", "mid", "fwd"])
    c.add_argument("--budget", type=float)
    c.add_argument("--gw", type=int)
    c.add_argument("--horizon", type=int, default=5)
    c.add_argument("--min-minutes", type=int, default=0)
    c.add_argument("--scan", type=int, default=40)
    c.add_argument("--top", type=int, default=15)
    c.set_defaults(fn=cmd_transfers)

    d = sub.add_parser("chips")
    d.add_argument("--gw", type=int)
    d.add_argument("--horizon", type=int, default=6)
    d.set_defaults(fn=cmd_chips)

    args = ap.parse_args()
    try:
        args.fn(args)
    except requests.HTTPError as e:
        sys.exit(f"API error: {e}\nCheck the team ID and that the gameweek has started.")


if __name__ == "__main__":
    main()
