#!/usr/bin/env python3
"""
Regression test: a flagged starter the model ranks below the bench must be benched.

GW6 2026/27: Joao Pedro carried "Knee injury - 75% chance of playing" for six
days while starting, with three healthy bench players the model scored higher.
Nothing moved. Three defects stacked:

  1. BENCH_THRESHOLD = 75 was documented "CoP below this should not start" but
     compared with `cop < 75`, so a cop of exactly 75 -- the most common value
     FPL publishes -- fell through the check entirely.

  2. plan_lineup returned before ranking anything unless a starter was
     UNPLAYABLE (cop <= 25). A player who merely should not start was never
     reconsidered, so the demotion in the scoring function could never fire.

  3. minutes_profile multiplied by cop/100 AND by 0.5 for a non-available
     status, double-counting one doubt: a stated 75% chance became 37.5%.

Defects 1 and 2 kept him starting; 3 made him look twice as bad as he was, so
the fix has to address all three or the lineup moves for the wrong reason.

    python test_flagged_starter.py
"""

from __future__ import annotations

import sys

import fpl_auto
import fpl_model as M

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILED.append(name)


def fake_player(pid, etype, cost, name, cop=None, status="a", starts=5, minutes=430):
    return {
        "id": pid, "element_type": etype, "now_cost": cost, "web_name": name,
        "chance_of_playing_next_round": cop, "chance_of_playing_this_round": cop,
        "status": status, "news": "", "starts": starts, "minutes": minutes,
        "team": 1 + (pid % 4), "selected_by_percent": "5.0",
        "expected_goals": "1.0", "expected_assists": "0.5",
        "expected_goal_involvements": "1.5", "expected_goals_conceded": "5.0",
        "clean_sheets": 2, "goals_scored": 1, "assists": 1, "bonus": 5,
        "saves": 0, "penalties_saved": 0, "yellow_cards": 1, "red_cards": 0,
        "own_goals": 0, "goals_conceded": 5, "total_points": 25, "form": "3.0",
        "defensive_contribution": 30, "points_per_game": "4.0", "minutes_per_game": 86,
    }


def test_double_discount():
    """A stated 75% chance must stay 75%, not be halved again to 37.5%."""
    print("\n[1] minutes_profile does not double-count one doubt")
    M._PLAYED[0] = 5
    flagged = fake_player(1, 4, 77, "Flagged", cop=75, status="d", starts=4, minutes=340)
    healthy = fake_player(2, 4, 77, "Healthy", cop=None, status="a", starts=4, minutes=340)

    p_flag, _, _ = M.minutes_profile(flagged)
    p_well, _, _ = M.minutes_profile(healthy)
    ratio = p_flag / p_well if p_well else 0

    print(f"        p_start healthy={p_well:.3f} flagged={p_flag:.3f} ratio={ratio:.3f}")
    check("75% cop scales p_start by ~0.75", round(ratio, 2), 0.75)

    # A flag with no percentage still has to be discounted by something.
    vague = fake_player(3, 4, 77, "Vague", cop=None, status="d", starts=4, minutes=340)
    p_vague, _, _ = M.minutes_profile(vague)
    check("flag with no cop still discounted", p_vague < p_well, True)


def test_boundary():
    """cop of exactly 75 is the commonest FPL flag and must be caught."""
    print("\n[2] a cop of exactly 75 counts as doubtful")
    check("75 is treated as doubtful", fpl_auto.is_doubtful(75), True)
    check("100 is not doubtful", fpl_auto.is_doubtful(100), False)
    check("50 is doubtful", fpl_auto.is_doubtful(50), True)
    check("25 is out, not merely doubtful", fpl_auto.is_out(25), True)


def test_gate_reconsiders_flagged_starter():
    """
    The gate must open for a flagged starter the model ranks below the bench --
    that is the whole failure. It must stay shut for a healthy XI.
    """
    print("\n[3] plan_lineup acts on a flagged starter beaten by the bench")

    elements, picks, ep = {}, [], {}
    # 11 healthy starters, the last of them flagged at 75% and low-scoring.
    layout = [(1, "GK1", 6.0, 4.5), (2, "DEF1", 5.5, 4.2), (2, "DEF2", 5.0, 4.0),
              (2, "DEF3", 5.0, 3.8), (3, "MID1", 12.0, 5.0), (3, "MID2", 8.0, 4.8),
              (3, "MID3", 7.0, 3.9), (4, "FWD1", 15.0, 5.2), (4, "FWD2", 6.0, 3.0),
              (2, "DEF4", 4.5, 2.6)]
    pid = 1
    for etype, nm, cost, epv in layout:
        elements[pid] = fake_player(pid, etype, int(cost * 10), nm)
        picks.append({"element": pid, "position": len(picks) + 1,
                      "is_captain": nm == "FWD1", "is_vice_captain": nm == "MID1"})
        ep[pid] = epv
        pid += 1
    # the flagged starter, 11th
    flagged_id = pid
    elements[pid] = fake_player(pid, 4, 77, "FLAGGED", cop=75, status="d")
    picks.append({"element": pid, "position": 11, "is_captain": False,
                  "is_vice_captain": False})
    ep[pid] = 1.5
    pid += 1
    # bench: one clearly better midfielder plus filler
    better_id = pid
    for etype, nm, epv in [(3, "BENCHBEST", 3.3), (1, "GK2", 0.4), (2, "DEF5", 2.1),
                           (3, "MID4", 2.7)]:
        elements[pid] = fake_player(pid, etype, 50, nm)
        picks.append({"element": pid, "position": len(picks) + 1,
                      "is_captain": False, "is_vice_captain": False})
        ep[pid] = epv
        pid += 1

    fixtures = {t: [{"difficulty": 3, "opp": "OPP", "ha": "H", "gw": 6}]
                for t in range(1, 6)}
    team = {"picks": picks}

    payload, changes = fpl_auto.plan_lineup(team, elements, fixtures, ep)
    print(f"        changes: {changes}")
    check("a change is proposed", payload is not None, True)
    if payload:
        started = {p["element"] for p in payload[:11]}
        check("flagged starter is benched", flagged_id not in started, True)
        check("better bench player starts", better_id in started, True)

    # control: nothing flagged, nothing should move
    for p in elements.values():
        p["status"], p["chance_of_playing_next_round"] = "a", None
    payload2, _ = fpl_auto.plan_lineup(team, elements, fixtures, ep)
    check("healthy XI is left alone", payload2 is None, True)


if __name__ == "__main__":
    test_double_discount()
    test_boundary()
    test_gate_reconsiders_flagged_starter()
    print("\n" + ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    sys.exit(1 if FAILED else 0)
