#!/usr/bin/env python3
"""
Create your FPL team from the planned squad, then set the opening lineup.

    python fpl_create.py --name "My Team" --club MUN           # dry run, validates only
    python fpl_create.py --name "My Team" --club MUN --confirm  # actually creates it
    python fpl_create.py --clubs                                # list favourite-club codes

The squad comes from squad_and_fixtures.json if you have one, otherwise it is
built from the expected-points model. Before sending anything it checks
budget, squad composition, the 3-per-club cap and every player's availability --
if any of those fail, nothing is sent.

Creating an entry is ONE-SHOT: you cannot delete a team and start over. The squad
itself stays fully editable until the GW1 deadline, and the name and favourite
club can be changed later, so the irreversible part is only the entry existing.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fpl_write

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
REQUIRED = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
BUDGET = 1000  # tenths
MAX_PER_CLUB = 3


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").lower()


def bootstrap():
    f = STATE / "cache_bootstrap.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    import requests
    r = requests.get("https://fantasy.premierleague.com/api/bootstrap-static/",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    STATE.mkdir(exist_ok=True)
    f.write_text(json.dumps(r.json()), encoding="utf-8")
    return r.json()


def optimal_squad(boot):
    """Build a squad from the expected-points model. Used when no plan file exists."""
    import fpl_model as M
    import fpl_optimise as O
    _, fixtures, _ = M.build_fixtures(6)
    rows = M.score_all(boot, fixtures, 6)
    squad, xi, cap = O.solve(rows)
    out = []
    for r in sorted(squad, key=lambda r: (r["p"]["id"] not in xi, r["p"]["element_type"])):
        out.append((r["p"]["id"], {
            "name": r["p"]["web_name"],
            "start": r["p"]["id"] in xi,
            "bench_order": None if r["p"]["id"] in xi else 0,
        }))
    return out, cap


def resolve(boot):
    """Map planned squad names -> live element ids."""
    teams = {t["id"]: t for t in boot["teams"]}
    plan_file = ROOT / "squad_and_fixtures.json"
    if not plan_file.exists():
        return None
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    entries = plan["squad"]["players"]
    cache = json.loads((STATE / "squad_ids.json").read_text(encoding="utf-8")) \
        if (STATE / "squad_ids.json").exists() else {}

    out, missing = [], []
    for e in entries:
        key = f"{e['name']}|{e['club']}"
        pid = cache.get(key)
        if not pid:
            target = strip_accents(e["name"])
            surname = target.split()[-1]
            pool = [p for p in boot["elements"]
                    if teams[p["team"]]["short_name"] == e["club"]]
            hit = next((p for p in pool
                        if strip_accents(f"{p['first_name']} {p['second_name']}") == target
                        or strip_accents(p["web_name"]) == target), None)
            if not hit:
                hit = next((p for p in pool
                            if surname in strip_accents(f"{p['first_name']} {p['second_name']}").split()
                            or surname in strip_accents(p["web_name"])), None)
            if not hit:
                missing.append(key)
                continue
            pid = hit["id"]
        out.append((pid, e))
    if missing:
        sys.exit("could not resolve: " + ", ".join(missing))
    return out


def validate(squad, elements, teams):
    errors, total = [], 0
    counts, clubs = defaultdict(int), defaultdict(list)

    for pid, entry in squad:
        p = elements[pid]
        total += p["now_cost"]
        counts[POS[p["element_type"]]] += 1
        clubs[teams[p["team"]]["short_name"]].append(p["web_name"])
        if p["status"] != "a":
            errors.append(f"{p['web_name']} is unavailable (status '{p['status']}'"
                          f"{' - ' + p['news'] if p['news'] else ''})")

    if len(squad) != 15:
        errors.append(f"squad has {len(squad)} players, need 15")
    for pos, need in REQUIRED.items():
        if counts[pos] != need:
            errors.append(f"{pos}: have {counts[pos]}, need {need}")
    if total > BUDGET:
        errors.append(f"over budget by {(total - BUDGET)/10:.1f}m")
    for club, names in clubs.items():
        if len(names) > MAX_PER_CLUB:
            errors.append(f"{len(names)} from {club} ({', '.join(names)}) - max {MAX_PER_CLUB}")
    return errors, total, counts, clubs


def build_lineup(squad, elements, cap_hint=None):
    """Starters first, then bench in order. Captain from the plan, or the model."""
    starters = [(pid, e) for pid, e in squad if e.get("start")]
    bench = sorted([(pid, e) for pid, e in squad if not e.get("start")],
                   key=lambda x: x[1].get("bench_order", 9))

    plan_file = ROOT / "squad_and_fixtures.json"
    if plan_file.exists():
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        cap_name = strip_accents(plan["squad"].get("captain_gw1", "")).split()[-1]
        vice_name = strip_accents(plan["squad"].get("vice_gw1", "")).split()[-1]
    else:
        cap_name = strip_accents(elements[cap_hint]["web_name"]) if cap_hint else ""
        vice_name = ""

    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    starters.sort(key=lambda x: order[POS[elements[x[0]]["element_type"]]])

    cap = vice = None
    for pid, _ in starters:
        web = strip_accents(elements[pid]["web_name"])
        full = strip_accents(f"{elements[pid]['first_name']} {elements[pid]['second_name']}")
        if cap_name and (cap_name in web or cap_name in full):
            cap = pid
        if vice_name and (vice_name in web or vice_name in full):
            vice = pid
    if cap is None:
        cap = starters[-1][0]
    if vice is None or vice == cap:
        vice = next(pid for pid, _ in starters if pid != cap)

    picks = []
    for i, (pid, _) in enumerate(starters, start=1):
        picks.append({"element": pid, "position": i,
                      "is_captain": pid == cap, "is_vice_captain": pid == vice})
    for i, (pid, _) in enumerate(bench, start=12):
        picks.append({"element": pid, "position": i,
                      "is_captain": False, "is_vice_captain": False})
    return picks, cap, vice


def main():
    ap = argparse.ArgumentParser(description="create your FPL team")
    ap.add_argument("--name", help="team name, max 20 chars")
    ap.add_argument("--club", help="favourite club short code, or NONE")
    ap.add_argument("--confirm", action="store_true", help="actually create the team")
    ap.add_argument("--clubs", action="store_true", help="list club codes and exit")
    args = ap.parse_args()

    boot = bootstrap()
    elements = {p["id"]: p for p in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    by_code = {t["short_name"]: t["id"] for t in boot["teams"]}

    if args.clubs:
        for t in sorted(boot["teams"], key=lambda t: t["short_name"]):
            print(f"  {t['short_name']:5} {t['name']}")
        return

    if not args.name:
        sys.exit("--name is required (max 20 characters)")
    if len(args.name) > 20:
        sys.exit(f"team name is {len(args.name)} chars, max is 20")

    fav = None
    if args.club and args.club.upper() != "NONE":
        fav = by_code.get(args.club.upper())
        if not fav:
            sys.exit(f"unknown club '{args.club}'. Run --clubs to list them.")

    cap_hint = None
    squad = resolve(boot)
    if squad is None:
        print("No squad_and_fixtures.json found - building the optimal squad "
              "from the expected-points model instead.\n")
        squad, cap_hint = optimal_squad(boot)
    errors, total, counts, clubs = validate(squad, elements, teams)

    print(f"\nTeam name      : {args.name}")
    print(f"Favourite club : {teams[fav]['name'] if fav else 'none'}")
    print(f"Squad value    : {total/10:.1f}m / {BUDGET/10:.1f}m "
          f"(bank {(BUDGET-total)/10:.1f}m)")
    print(f"Composition    : " + " / ".join(f"{counts[p]} {p}" for p in ["GK", "DEF", "MID", "FWD"]))
    print(f"Clubs          : " + ", ".join(f"{c}x{len(n)}" for c, n in sorted(clubs.items()) if len(n) > 1))

    picks, cap, vice = build_lineup(squad, elements, cap_hint)
    print(f"\nStarting XI (captain {elements[cap]['web_name']}, "
          f"vice {elements[vice]['web_name']}):")
    for pk in picks[:11]:
        p = elements[pk["element"]]
        tag = " (C)" if pk["is_captain"] else " (V)" if pk["is_vice_captain"] else ""
        print(f"  {POS[p['element_type']]:4}{p['web_name']:<18}"
              f"{teams[p['team']]['short_name']:<5}{p['now_cost']/10:>5.1f}{tag}")
    print("Bench:")
    for pk in picks[11:]:
        p = elements[pk["element"]]
        print(f"  {pk['position']-11}. {POS[p['element_type']]:4}{p['web_name']:<18}"
              f"{teams[p['team']]['short_name']:<5}{p['now_cost']/10:>5.1f}")

    if errors:
        print("\nBLOCKED - nothing sent:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print("\nvalidation: OK (budget, composition, 3-per-club, availability)")

    create_payload = {
        "email": False,
        "favourite_team": fav,
        "name": args.name,
        "terms_agreed": True,
        "picks": [{"element": pid, "purchase_price": elements[pid]["now_cost"]}
                  for pid, _ in squad],
    }

    if not args.confirm:
        print("\nDRY RUN - nothing sent. Re-run with --confirm to create the team.")
        print("payload preview:")
        print(json.dumps({**create_payload, "picks": create_payload["picks"][:2] + ["...13 more"]},
                         indent=2))
        return

    s = fpl_write.session()
    entry, player = fpl_write.whoami(s)
    if entry:
        sys.exit(f"You already have a team (entry {entry}). "
                 f"Use fpl_daily.py to manage it instead.")

    print(f"\ncreating team for {player.get('first_name')} {player.get('last_name')}...")
    r = s.post("https://fantasy.premierleague.com/api/entry-create/",
               json=create_payload, timeout=30)
    fpl_write._audit("entry_create", create_payload, r.status_code, r.text)
    if r.status_code >= 400:
        sys.exit(f"REJECTED HTTP {r.status_code}: {r.text[:500]}")

    entry, _ = fpl_write.whoami(s)
    if not entry:
        sys.exit("created, but /api/me/ still shows no entry. Check the site.")
    print(f"team created — entry id {entry}")

    cfg_path = STATE / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg["team_id"] = entry
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    try:
        fpl_write.set_lineup(entry, picks, s=s)
        print("lineup, captain and bench order set.")
    except Exception as e:
        print(f"! team created but lineup write failed: {e}")
        print("  run: python fpl_daily.py --apply")


if __name__ == "__main__":
    main()
