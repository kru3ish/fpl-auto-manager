#!/usr/bin/env python3
"""
Rank-aware scoring. The piece the expected-points model is missing.

FPL does not pay out on points. It pays out on RANK. Those objectives diverge,
and the divergence is the whole game:

  Captain Haaland (70% owned, ~40% captained -> effective ownership ~110%) and
  he hauls 20 points. You scored 40. So did most of the field. Your rank barely
  moves. He blanks instead? Also fine -- everyone blanked with you.

  Captain a 6% differential and he hauls. You scored 40 and almost nobody else
  did. You climb hard. He blanks? You fall hard.

Owning the template is not safety, it is *stasis* -- you move with the crowd,
in both directions. From two and a half million, stasis is losing slowly.

The unit here is EXPECTED RANK GAIN, not expected points:

    rank_gain = (my_multiplier - effective_ownership) x expected_points

A player you own at 1x who is 100% effectively owned contributes ZERO rank
movement no matter how many points he scores. That is the number the EP model
cannot see, and it is why it would never suggest a differential.

    python fpl_rank.py                 # squad template analysis
    python fpl_rank.py --captain       # captaincy ranked by rank gain, not points
    python fpl_rank.py --differentials # best low-owned picks by rank leverage
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fpl_model as M

ROOT = Path(__file__).resolve().parent

# Captaincy is not in the API. It concentrates hard on popular, high-scoring
# players, so we distribute a notional 100% of armbands across the league in
# proportion to ownership x EP^3 -- the cube makes it concentrate the way real
# captaincy does rather than spreading thinly across every owned player.
CAPTAIN_CONCENTRATION = 3.0


def effective_ownership(rows):
    """
    EO = ownership + captaincy. Captains count twice, so a captained player is
    'owned' at more than 100% by the field.
    """
    weights, total = {}, 0.0
    for r in rows:
        own = float(r["p"]["selected_by_percent"] or 0)
        if own < 0.5 or r["ep_game"] <= 0:
            continue
        w = own * (r["ep_game"] ** CAPTAIN_CONCENTRATION)
        weights[r["p"]["id"]] = w
        total += w

    eo = {}
    for r in rows:
        own = float(r["p"]["selected_by_percent"] or 0)
        cap = 100.0 * weights.get(r["p"]["id"], 0.0) / total if total else 0.0
        eo[r["p"]["id"]] = {"own": own, "captaincy": cap, "eo": own + cap}
    return eo


def rank_gain(my_multiplier, eo_pct, ep):
    """
    Expected rank movement, in 'points ahead of the field'.

    my_multiplier is 100 for a starter, 200 for a captain, 0 for bench/not-owned.
    A 1x holding of a 100%-EO player nets exactly zero — you gain what everyone
    else gains.
    """
    return (my_multiplier - eo_pct) / 100.0 * ep


def analyse(horizon=6):
    boot, fixtures, gw = M.build_fixtures(horizon)
    rows = M.score_all(boot, fixtures, horizon)
    eo = effective_ownership(rows)
    mine = M.owned_ids()
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    priors = M.positional_priors(boot)
    ratings = M.team_ratings(boot)
    return boot, rows, eo, mine, teams, priors, ratings, gw


def gw1_ep(r, priors, ratings):
    """Expected points for the NEXT gameweek only — the captaincy horizon."""
    if not r["fixtures"]:
        return 0.0
    ep, _, _ = M.expected_points(r["p"], [r["fixtures"][0]], priors, ratings)
    return ep


def main():
    ap = argparse.ArgumentParser(description="rank-aware FPL scoring")
    ap.add_argument("--captain", action="store_true")
    ap.add_argument("--differentials", action="store_true")
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--max-own", type=float, default=12.0,
                    help="ownership ceiling for a differential (default 12%%)")
    args = ap.parse_args()

    boot, rows, eo, mine, teams, priors, ratings, gw = analyse(args.horizon)
    by = {r["p"]["id"]: r for r in rows}

    # ---------------------------------------------------------------- captain
    if args.captain:
        print(f"\nGW{gw} CAPTAINCY — ranked by expected RANK GAIN, not points\n")
        print(f"  {'PLAYER':<16}{'OPP':<9}{'EP':>6}{'OWN%':>7}{'CAP%':>7}{'EO%':>7}"
              f"{'RANK GAIN':>11}")
        print("  " + "-" * 63)
        cands = []
        for pid in mine:
            r = by.get(pid)
            if not r or not r["fixtures"]:
                continue
            ep = gw1_ep(r, priors, ratings)
            e = eo[pid]
            cands.append((rank_gain(200, e["eo"], ep), ep, e, r))
        cands.sort(reverse=True, key=lambda x: x[0])
        for rg, ep, e, r in cands[:8]:
            f = r["fixtures"][0]
            print(f"  {r['p']['web_name']:<16}{f['opp'] + '(' + f['ha'] + ')':<9}"
                  f"{ep:>6.2f}{e['own']:>7.1f}{e['captaincy']:>7.1f}{e['eo']:>7.1f}"
                  f"{rg:>11.2f}")
        top_pts = max(cands, key=lambda x: x[1])
        top_rank = cands[0]
        print()
        print(f"  Most POINTS : {top_pts[3]['p']['web_name']} ({top_pts[1]:.2f} EP)")
        print(f"  Most RANK   : {top_rank[3]['p']['web_name']} ({top_rank[0]:.2f} rank gain)")
        if top_pts[3]["p"]["id"] != top_rank[3]["p"]["id"]:
            print("\n  These disagree. The safe captain scores more points; the")
            print("  differential moves you further up. Which is right depends on")
            print("  whether you are protecting a rank or chasing one.")
        else:
            print("\n  They agree — the highest-scoring option is also the best for rank.")
        return

    # ---------------------------------------------------- differential search
    if args.differentials:
        print(f"\nDIFFERENTIALS — under {args.max_own}% owned, by rank leverage\n")
        print(f"  {'PLAYER':<16}{'POS':<5}{'CLUB':<6}{'£':>5}{'EP/GW':>7}{'OWN%':>7}"
              f"{'RANK GAIN':>11}")
        print("  " + "-" * 60)
        cands = []
        for r in rows:
            p = r["p"]
            if p["id"] in mine or p["status"] != "a" or not r["fixtures"]:
                continue
            e = eo[p["id"]]
            if e["own"] > args.max_own or r["ep_game"] < 2.5:
                continue
            cands.append((rank_gain(100, e["eo"], r["ep_total"]), r, e))
        cands.sort(reverse=True, key=lambda x: x[0])
        for rg, r, e in cands[:15]:
            p = r["p"]
            print(f"  {p['web_name']:<16}{M.POS[p['element_type']]:<5}"
                  f"{teams[p['team']]:<6}{p['now_cost'] / 10:>5.1f}{r['ep_game']:>7.2f}"
                  f"{e['own']:>7.1f}{rg:>11.1f}")
        print("\n  Rank gain is over the whole horizon, at 1x. These are the players")
        print("  who move you up if they deliver — precisely because few others own them.")
        return

    # ------------------------------------------------------ template exposure
    print(f"\nGW{gw} TEMPLATE EXPOSURE — how much of your team is everyone's team\n")
    print(f"  {'PLAYER':<16}{'EP/GW':>7}{'OWN%':>7}{'EO%':>7}{'RANK GAIN':>11}  verdict")
    print("  " + "-" * 66)
    squad = sorted((by[i] for i in mine if i in by), key=lambda r: -r["ep_game"])
    xi = {r["p"]["id"] for r in squad[:11]}
    total_rg, template_ep, diff_ep = 0.0, 0.0, 0.0
    for r in squad:
        pid = r["p"]["id"]
        e = eo[pid]
        mult = 100 if pid in xi else 0
        rg = rank_gain(mult, e["eo"], r["ep_total"])
        total_rg += rg
        if e["eo"] >= 30:
            template_ep += r["ep_total"] if pid in xi else 0
            verdict = "template"
        elif e["eo"] >= 10:
            verdict = "semi"
        else:
            diff_ep += r["ep_total"] if pid in xi else 0
            verdict = "differential"
        tag = "" if pid in xi else " (bench)"
        print(f"  {r['p']['web_name'] + tag:<16}{r['ep_game']:>7.2f}{e['own']:>7.1f}"
              f"{e['eo']:>7.1f}{rg:>11.1f}  {verdict}")

    xi_ep = sum(r["ep_total"] for r in squad if r["p"]["id"] in xi)
    print("  " + "-" * 66)
    print(f"  XI expected points        {xi_ep:>8.1f}")
    print(f"  XI expected RANK gain     {total_rg:>8.1f}")
    if xi_ep:
        print(f"  template share of XI EP   {100 * template_ep / xi_ep:>7.0f}%")
        print(f"  differential share        {100 * diff_ep / xi_ep:>7.0f}%")
    print()
    print("  A high template share means you rise and fall with the crowd. That is")
    print("  fine if you are defending a good rank and fatal if you are chasing one.")


if __name__ == "__main__":
    main()
