#!/usr/bin/env python3
"""
What the other eleven million managers are actually doing.

Pundits predict; the transfer market reveals. bootstrap-static carries
transfers_in_event and transfers_out_event per player -- the real, settled
behaviour of the entire playerbase this gameweek, for free, with no opinion in
it. That is a strictly better answer to "what is everyone doing" than any column
or podcast, and it was sitting unused in a field the model already downloads.

It matters because FPL pays out on RANK, so a move only helps if it is not the
move everyone else is making:

  BANDWAGON   heavy net buys of a player you do NOT own. Every gameweek he
              hauls without you, you fall. This is the cost of standing still.

  EXODUS      heavy net sells of a player you DO own. His effective ownership
              is dropping, which quietly turns him into a differential -- good
              if the crowd is wrong, a warning if it knows something you don't.
              Cross-check an exodus against the news layer before trusting it.

Momentum is net transfers as a share of the players who currently own him, not
a raw count. 200k buys on a 40%-owned player is noise; the same number on a
2%-owned player is the crowd arriving all at once.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

# Total FPL playerbase, used to turn an ownership percentage into a headcount.
# Only the ratio matters, so an approximate figure is fine.
PLAYERBASE = 11_000_000

# A move has to shift this share of a player's existing owners before it counts
# as the crowd moving rather than ordinary churn.
MOMENTUM_FLOOR = 0.02

# ...but momentum alone is a ratio with a tiny denominator, and ranking on it
# put a 0.0%-owned backup keeper with 1,860 transfers at +186,000%. An absolute
# floor first, momentum second: the share is only meaningful once enough people
# have actually moved. Same failure as judging a striker on two minutes of xG.
MIN_NET_ABS = 25_000


def _owners(p):
    return max(1.0, float(p.get("selected_by_percent") or 0) / 100.0 * PLAYERBASE)


def flows(boot, mine, top=6):
    """Bandwagons you are missing and exoduses you are caught in."""
    owned = set(mine)
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    pos = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    rows = []
    for p in boot["elements"]:
        net = (p.get("transfers_in_event", 0) or 0) - (p.get("transfers_out_event", 0) or 0)
        if not net:
            continue
        momentum = net / _owners(p)
        if abs(net) < MIN_NET_ABS or abs(momentum) < MOMENTUM_FLOOR:
            continue
        rows.append({
            "id": p["id"], "name": p["web_name"], "club": teams.get(p["team"], "?"),
            "pos": pos.get(p["element_type"], "?"), "cost": p["now_cost"] / 10,
            "own": float(p.get("selected_by_percent") or 0),
            "net": net, "momentum": momentum, "mine": p["id"] in owned,
            "status": p["status"], "news": p.get("news") or "",
        })

    bandwagon = sorted([r for r in rows if r["net"] > 0 and not r["mine"]],
                       key=lambda r: -r["momentum"])[:top]
    # Rank an exodus by how many people left, not by share. A 66%-owned player
    # losing a quarter of a million owners is the loudest signal on the board
    # even though that is only 4% of them -- ranking on share buried it.
    exodus = sorted([r for r in rows if r["net"] < 0 and r["mine"]],
                    key=lambda r: r["net"])[:top]
    return {"bandwagon": bandwagon, "exodus": exodus}


def squad_flow(boot, mine):
    """Net movement on every player you own, so a quiet sell-off is visible."""
    owned = set(mine)
    out = []
    for p in boot["elements"]:
        if p["id"] not in owned:
            continue
        net = (p.get("transfers_in_event", 0) or 0) - (p.get("transfers_out_event", 0) or 0)
        out.append({"name": p["web_name"], "net": net,
                    "momentum": net / _owners(p),
                    "own": float(p.get("selected_by_percent") or 0)})
    return sorted(out, key=lambda r: r["momentum"])


if __name__ == "__main__":
    import fpl_model as M

    boot, _fx, gw = M.build_fixtures(1)
    mine = M.owned_ids()
    f = flows(boot, mine)

    print(f"\nGW{gw} CROWD MOVEMENT\n")
    print("  BANDWAGONS you are not on")
    print(f"    {'PLAYER':<16}{'POS':<5}{'CLUB':<6}{'£':>5}{'OWN%':>7}{'NET':>11}{'MOMENTUM':>10}")
    for r in f["bandwagon"]:
        print(f"    {r['name']:<16}{r['pos']:<5}{r['club']:<6}{r['cost']:>5.1f}"
              f"{r['own']:>7.1f}{r['net']:>+11,}{r['momentum']:>+9.0%}")

    print("\n  EXODUS from players you own")
    if not f["exodus"]:
        print("    nobody in your squad is being sold in numbers.")
    for r in f["exodus"]:
        print(f"    {r['name']:<16}{r['pos']:<5}{r['club']:<6}{r['cost']:>5.1f}"
              f"{r['own']:>7.1f}{r['net']:>+11,}{r['momentum']:>+9.0%}"
              + (f"  [{r['news'][:40]}]" if r["news"] else ""))

    print("\n  YOUR SQUAD, by net movement")
    for r in squad_flow(boot, mine):
        print(f"    {r['name']:<16}{r['own']:>7.1f}%{r['net']:>+11,}{r['momentum']:>+9.0%}")
