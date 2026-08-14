#!/usr/bin/env python3
"""
Squad optimiser. Exact, not greedy.

Before the GW1 deadline transfers are unlimited and free, so the squad isn't
locked to incremental swaps -- the whole 15 can be re-picked. This solves that
as an integer program: maximise expected points of the STARTING XI over the
horizon, subject to every FPL rule, with the bench weighted low because bench
players mostly exist to be legal and cheap.

    python fpl_optimise.py                 # show the optimal squad vs yours
    python fpl_optimise.py --lock Haaland --lock B.Fernandes
    python fpl_optimise.py --apply         # transfer your squad to match

--apply only runs while transfers are free (before your first deadline, or on a
wildcard). It refuses to take a hit, ever.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pulp

import fpl_model as M

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
POS = M.POS

SQUAD = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}
BENCH_WEIGHT = 0.12          # a bench slot is worth ~1/8 of a starting slot
MAX_PER_CLUB = 3
BUDGET = 1000


def solve(rows, budget=BUDGET, locked=(), banned=(), min_minutes_for_xi=45):
    """Integer program: pick 15, start 11, maximise XI expected points."""
    pool = [r for r in rows
            if r["p"]["status"] == "a"
            and r["fixtures"]
            and r["p"]["web_name"] not in banned]

    prob = pulp.LpProblem("fpl", pulp.LpMaximize)
    x, y, c = {}, {}, {}
    for r in pool:
        i = r["p"]["id"]
        x[i] = pulp.LpVariable(f"x{i}", cat="Binary")
        y[i] = pulp.LpVariable(f"y{i}", cat="Binary")
        c[i] = pulp.LpVariable(f"c{i}", cat="Binary")

    idx = {r["p"]["id"]: r for r in pool}

    # The captain scores twice. Leaving that out makes premiums look overpriced,
    # because their real value is (points x2) every single week.
    prob += pulp.lpSum(
        idx[i]["ep_total"] * y[i]
        + idx[i]["ep_total"] * c[i]
        + BENCH_WEIGHT * idx[i]["ep_total"] * (x[i] - y[i])
        for i in x)

    prob += pulp.lpSum(c.values()) == 1
    for i in x:
        prob += c[i] <= y[i]

    prob += pulp.lpSum(x.values()) == 15
    prob += pulp.lpSum(y.values()) == 11
    prob += pulp.lpSum(idx[i]["p"]["now_cost"] * x[i] for i in x) <= budget

    for i in x:
        prob += y[i] <= x[i]

    for pos, n in SQUAD.items():
        members = [i for i in x if POS[idx[i]["p"]["element_type"]] == pos]
        prob += pulp.lpSum(x[i] for i in members) == n
        prob += pulp.lpSum(y[i] for i in members) >= XI_MIN[pos]
        prob += pulp.lpSum(y[i] for i in members) <= XI_MAX[pos]

    clubs = defaultdict(list)
    for i in x:
        clubs[idx[i]["p"]["team"]].append(i)
    for team, members in clubs.items():
        prob += pulp.lpSum(x[i] for i in members) <= MAX_PER_CLUB

    # don't start someone who barely plays
    for i in x:
        if M.expected_minutes(idx[i]["p"]) < min_minutes_for_xi:
            prob += y[i] == 0

    for name in locked:
        hits = [i for i in x if idx[i]["p"]["web_name"] == name]
        if not hits:
            sys.exit(f"--lock {name}: no such available player")
        prob += pulp.lpSum(x[i] for i in hits) == 1

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        sys.exit(f"solver returned {pulp.LpStatus[status]}")

    squad = [idx[i] for i in x if x[i].value() > 0.5]
    xi = {i for i in y if y[i].value() > 0.5}
    cap = next((i for i in c if c[i].value() > 0.5), None)
    return squad, xi, cap


def show(squad, xi, teams, mine, label, cap_id=None):
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    squad = sorted(squad, key=lambda r: (r["p"]["id"] not in xi,
                                         order[POS[r["p"]["element_type"]]],
                                         -r["ep_game"]))
    cost = sum(r["p"]["now_cost"] for r in squad)
    ep_xi = sum(r["ep_total"] for r in squad if r["p"]["id"] in xi)
    if cap_id is None:
        starters = [r for r in squad if r["p"]["id"] in xi]
        cap_id = max(starters, key=lambda r: r["ep_total"])["p"]["id"] if starters else None
    ep_xi += next((r["ep_total"] for r in squad if r["p"]["id"] == cap_id), 0.0)

    print(f"\n{label}   £{cost/10:.1f}m   XI expected points (capt. doubled): {ep_xi:.1f}")
    print(f"{'':3}{'PLAYER':<17}{'POS':<4}{'TEAM':<5}{'£':>5}{'EP/GW':>7}{'DefC':>6}{'Own%':>7}")
    print("-" * 58)
    for r in squad:
        p = r["p"]
        start = p["id"] in xi
        mark = "*" if p["id"] in mine else " "
        tag = "  " if start else "B "
        dc = r["breakdown"].get("defcon", 0) / max(1, len(r["fixtures"]))
        if p["id"] == cap_id:
            tag = "C "
        print(f"{mark}{tag}{p['web_name']:<17}{POS[p['element_type']]:<4}"
              f"{teams[p['team']]['short_name']:<5}{p['now_cost']/10:>5.1f}"
              f"{r['ep_game']:>7.2f}{dc:>6.2f}{float(p['selected_by_percent']):>7.1f}")
    return ep_xi, cost


def main():
    ap = argparse.ArgumentParser(description="FPL squad optimiser")
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--lock", action="append", default=[],
                    help="force a player into the squad (repeatable)")
    ap.add_argument("--ban", action="append", default=[])
    ap.add_argument("--apply", action="store_true", help="execute the transfers")
    args = ap.parse_args()

    boot, fixtures, gw = M.build_fixtures(args.horizon)
    teams = {t["id"]: t for t in boot["teams"]}
    rows = M.score_all(boot, fixtures, args.horizon)
    by_id = {r["p"]["id"]: r for r in rows}
    mine = M.owned_ids()

    cur = [by_id[i] for i in mine if i in by_id]
    cur_xi = {r["p"]["id"] for r in sorted(cur, key=lambda r: -r["ep_game"])[:11]}
    cur_ep, cur_cost = show(cur, cur_xi, teams, mine, "YOUR SQUAD")

    squad, xi, cap_id = solve(rows, locked=args.lock, banned=args.ban)
    opt_ep, opt_cost = show(squad, xi, teams, mine, "OPTIMAL SQUAD", cap_id)

    keep = {r["p"]["id"] for r in squad} & mine
    out = [by_id[i] for i in mine - keep]
    into = [r for r in squad if r["p"]["id"] not in mine]

    print(f"\nGAIN: {opt_ep - cur_ep:+.1f} expected points over {args.horizon} gameweeks "
          f"({(opt_ep - cur_ep)/args.horizon:+.2f} per GW)")
    print(f"CHANGES: {len(out)} out, {len(into)} in\n")
    for o, i in zip(sorted(out, key=lambda r: -r["p"]["now_cost"]),
                    sorted(into, key=lambda r: -r["p"]["now_cost"])):
        print(f"  OUT {o['p']['web_name']:<17}{o['p']['now_cost']/10:>5.1f} "
              f"({o['ep_game']:.2f})   ->  IN {i['p']['web_name']:<17}"
              f"{i['p']['now_cost']/10:>5.1f} ({i['ep_game']:.2f})")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to execute.")
        return
    if not into:
        print("\nNothing to do.")
        return

    import fpl_write
    s = fpl_write.session()
    entry, _ = fpl_write.whoami(s)
    team = fpl_write.my_team(entry, s)
    tr = team["transfers"]
    unlimited = tr["limit"] is None or tr.get("status") == "unlimited"
    if not unlimited:
        free = tr["limit"] - tr["made"]
        if len(into) > free:
            sys.exit(f"REFUSED: {len(into)} transfers needed but only {free} free. "
                     f"That's a {(len(into)-free)*4} point hit.")

    sell = {pk["element"]: pk["selling_price"] for pk in team["picks"]}
    # FPL requires each transfer to swap like for like: a DEF can only be
    # replaced by a DEF. Pair within position, not across the whole list.
    out_by_pos, in_by_pos = defaultdict(list), defaultdict(list)
    for r in out:
        out_by_pos[r["p"]["element_type"]].append(r)
    for r in into:
        in_by_pos[r["p"]["element_type"]].append(r)

    moves = []
    for et, outs in out_by_pos.items():
        ins = in_by_pos.get(et, [])
        if len(outs) != len(ins):
            sys.exit(f"position mismatch for {POS[et]}: {len(outs)} out vs {len(ins)} in")
        outs.sort(key=lambda r: -r["p"]["now_cost"])
        ins.sort(key=lambda r: -r["p"]["now_cost"])
        for o, i in zip(outs, ins):
            moves.append({"element_in": i["p"]["id"], "element_out": o["p"]["id"],
                          "purchase_price": i["p"]["now_cost"],
                          "selling_price": sell[o["p"]["id"]]})

    print(f"\nsending {len(moves)} transfers...")
    fpl_write.make_transfers(entry, gw, moves, s=s)
    print("transfers applied.")

    team = fpl_write.my_team(entry, s)
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    live = [by_id[pk["element"]] for pk in team["picks"] if pk["element"] in by_id]
    starters = sorted([r for r in live if r["p"]["id"] in xi],
                      key=lambda r: order[POS[r["p"]["element_type"]]])
    bench = sorted([r for r in live if r["p"]["id"] not in xi],
                   key=lambda r: (POS[r["p"]["element_type"]] != "GK", -r["ep_game"]))
    if len(starters) != 11:
        print(f"! expected 11 starters, got {len(starters)} - run fpl_daily.py --apply")
        return
    cap = max(starters, key=lambda r: r["ep_game"])
    vice = max((r for r in starters if r is not cap), key=lambda r: r["ep_game"])
    picks = []
    for n, r in enumerate(starters, 1):
        picks.append({"element": r["p"]["id"], "position": n,
                      "is_captain": r is cap, "is_vice_captain": r is vice})
    for n, r in enumerate(bench, 12):
        picks.append({"element": r["p"]["id"], "position": n,
                      "is_captain": False, "is_vice_captain": False})
    fpl_write.set_lineup(entry, picks, s=s)
    print(f"lineup set. captain {cap['p']['web_name']}, vice {vice['p']['web_name']}")


if __name__ == "__main__":
    main()
