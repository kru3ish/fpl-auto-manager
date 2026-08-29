#!/usr/bin/env python3
"""
Expected-points model for FPL 2026/27.

The daily engine used to score players on `form` and FDR alone, which is useless
before a ball is kicked because `form` is zero for everyone. But the API ships
LAST season's full underlying numbers in bootstrap-static right now -- minutes,
starts, xG, xA, clean sheets, and the per-90 defensive-contribution rate. That's
real signal from day one.

This module turns those into an expected-points-per-gameweek figure, built from
the actual 2026/27 scoring rules rather than a vibes-based weighting.

    python fpl_model.py --squad          # score your current 15
    python fpl_model.py --pos DEF --max 6.0
    python fpl_model.py --upgrades       # best swap for each of your players
"""

from __future__ import annotations

import argparse
import json
import sys
import math
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

# --- 2026/27 scoring ---------------------------------------------------------
GOAL_PTS = {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4}
CS_PTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_PTS = 3
DEFCON_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12, "GK": 999}
DEFCON_PTS = 2

# Fixture difficulty -> multiplier on attacking output, and on clean-sheet odds.
ATTACK_MULT = {1: 1.35, 2: 1.20, 3: 1.00, 4: 0.82, 5: 0.68}
CS_MULT = {1: 1.55, 2: 1.30, 3: 1.00, 4: 0.70, 5: 0.50}


# --- team strength -----------------------------------------------------------
# FDR is a 1-5 bucket set by hand. Last season's underlying numbers give a real
# attack and defence rating per club, which turns fixture adjustment into a
# proper Poisson goals model instead of a lookup table.
LEAGUE_GOALS_PER_GAME = 1.45
HOME_ADV, AWAY_ADJ = 1.15, 0.87
PROMOTED_ATTACK, PROMOTED_DEFENCE = 0.72, 1.32   # COV / HUL / IPS have no PL data

_RATINGS: dict | None = None


def matches_played(boot):
    """League matches played so far. 38 in pre-season, when stats are last season's."""
    finished = sum(1 for e in boot["events"] if e.get("finished"))
    return 38 if finished == 0 else finished


def team_ratings(boot):
    """(attack, defence) multipliers per team id, 1.0 == league average."""
    global _RATINGS
    if _RATINGS is not None:
        return _RATINGS
    xg = defaultdict(float)
    gc_num, gc_den = defaultdict(float), defaultdict(float)
    for p in boot["elements"]:
        xg[p["team"]] += f(p["expected_goals"])
        if p["element_type"] in (1, 2) and p["minutes"] > 500:
            gc_num[p["team"]] += f(p["expected_goals_conceded_per_90"]) * p["minutes"]
            gc_den[p["team"]] += p["minutes"]

    # Thresholds must scale with how much football has been played. In pre-season
    # bootstrap-static carries LAST season's totals, so every club clears a fixed
    # bar. The moment GW1 finishes those fields reset to this season's numbers and
    # a fixed bar excludes everyone -- which used to divide by zero and take the
    # whole engine down. Scale the bar by matches played, and fall back to neutral
    # ratings when there genuinely isn't enough football yet to say anything.
    played = matches_played(boot)
    min_xg = max(0.5, 0.35 * played)
    established = [t["id"] for t in boot["teams"]
                   if gc_den[t["id"]] > 0 and xg[t["id"]] >= min_xg]

    if len(established) < 6:
        # too little signal: treat every club as league-average rather than guess
        _RATINGS = {t["id"]: (1.0, 1.0) for t in boot["teams"]}
        return _RATINGS

    scale = float(max(1, played))
    avg_xg = sum(xg[i] for i in established) / len(established) / scale
    avg_gc = sum(gc_num[i] / gc_den[i] for i in established) / len(established)
    if avg_xg <= 0 or avg_gc <= 0:
        _RATINGS = {t["id"]: (1.0, 1.0) for t in boot["teams"]}
        return _RATINGS

    out = {}
    for t in boot["teams"]:
        i = t["id"]
        if i in established:
            out[i] = ((xg[i] / scale) / avg_xg, (gc_num[i] / gc_den[i]) / avg_gc)
        else:
            out[i] = (PROMOTED_ATTACK, PROMOTED_DEFENCE)
    _RATINGS = out
    return out


def fixture_model(team_id, opp_id, is_home, ratings):
    """Expected goals for and against in one fixture -> (attack_mult, cs_prob)."""
    atk_t, def_t = ratings.get(team_id, (1.0, 1.0))
    atk_o, def_o = ratings.get(opp_id, (1.0, 1.0))
    gf = LEAGUE_GOALS_PER_GAME * atk_t * def_o * (HOME_ADV if is_home else AWAY_ADJ)
    ga = LEAGUE_GOALS_PER_GAME * atk_o * def_t * (AWAY_ADJ if is_home else HOME_ADV)
    attack_mult = gf / LEAGUE_GOALS_PER_GAME
    cs_prob = math.exp(-ga)          # Poisson P(opponent scores 0)
    return attack_mult, cs_prob, ga


def load(name):
    return json.loads((STATE / name).read_text(encoding="utf-8"))


def poisson_at_least(k, lam):
    """P(X >= k) for X ~ Poisson(lam). Used for defensive-contribution odds."""
    if lam <= 0:
        return 0.0
    if k <= 0:
        return 1.0
    cum = sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))
    return max(0.0, min(1.0, 1.0 - cum))


def f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --- regression to the mean --------------------------------------------------
# A player with 2 minutes and one shot has an xG90 of 3.6. Taken literally that
# makes him the best forward alive. Every per-90 rate therefore gets shrunk
# toward the positional average, weighted by how many minutes back it up.
# K is the prior strength in minutes: at K minutes played, a player's own rate
# and the positional average carry equal weight.
# Calibrated against last season's actual points per start (n=217 regulars):
# correlation peaks at K=100, but that test set contains only established
# starters, so it is biased toward less shrinkage -- small samples, the thing
# shrinkage exists to protect against, are absent from it by construction.
# K=250 keeps 0.864 of the 0.867 peak while still giving a 250-minute player
# a 50/50 blend with the positional mean.
SHRINK_K = 250
REGULAR_MINUTES = 900   # who counts when computing the positional averages

_PRIORS: dict | None = None


def positional_priors(boot):
    """League-average per-90 rates by position, from regulars only."""
    global _PRIORS
    if _PRIORS is not None:
        return _PRIORS
    # Same problem as team_ratings: a fixed 900-minute bar matches nobody once
    # the season resets the counters. Scale it to the football actually played.
    played = matches_played(boot)
    bar = max(60, int(REGULAR_MINUTES * played / 38.0))
    acc = defaultdict(lambda: defaultdict(list))
    for p in boot["elements"]:
        if p["minutes"] < bar or p["starts"] == 0:
            continue
        pos = POS[p["element_type"]]
        acc[pos]["xg90"].append(f(p["expected_goals_per_90"]))
        acc[pos]["xa90"].append(f(p["expected_assists_per_90"]))
        acc[pos]["dc90"].append(f(p["defensive_contribution_per_90"]))
        acc[pos]["saves90"].append(f(p["saves_per_90"]))
        acc[pos]["gc90"].append(f(p["goals_conceded_per_90"]))
        acc[pos]["cs_rate"].append(p["clean_sheets"] / p["starts"])
        acc[pos]["bonus_rate"].append(p["bonus"] / p["starts"])
    _PRIORS = {pos: {k: (sum(v) / len(v) if v else 0.0) for k, v in d.items()}
               for pos, d in acc.items()}
    # If a position produced nothing at all, fall back to sane league defaults so
    # downstream shrinkage still has something to pull toward.
    DEFAULTS = {
        "GK":  {"xg90": 0.0, "xa90": 0.0, "dc90": 0.5, "saves90": 3.0,
                "gc90": 1.4, "cs_rate": 0.28, "bonus_rate": 0.25},
        "DEF": {"xg90": 0.06, "xa90": 0.06, "dc90": 7.8, "saves90": 0.0,
                "gc90": 1.4, "cs_rate": 0.28, "bonus_rate": 0.25},
        "MID": {"xg90": 0.16, "xa90": 0.13, "dc90": 8.4, "saves90": 0.0,
                "gc90": 1.4, "cs_rate": 0.28, "bonus_rate": 0.30},
        "FWD": {"xg90": 0.43, "xa90": 0.06, "dc90": 4.5, "saves90": 0.0,
                "gc90": 1.4, "cs_rate": 0.28, "bonus_rate": 0.60},
    }
    for pos, defaults in DEFAULTS.items():
        cur = _PRIORS.setdefault(pos, {})
        for k, v in defaults.items():
            if not cur.get(k):
                cur[k] = v
    return _PRIORS


def shrunk(p, field, prior_key, priors, k=SHRINK_K):
    """A player's per-90 rate, pulled toward the positional mean by sample size."""
    pos = POS[p["element_type"]]
    own = f(p.get(field))
    prior = priors.get(pos, {}).get(prior_key, 0.0)
    mins = p["minutes"]
    w = mins / (mins + k)
    return w * own + (1 - w) * prior


def minutes_profile(p):
    """
    (p_start, mins_if_start, p_60) -- kept separate on purpose.

    Averaging minutes and then testing them against the 60-minute threshold
    double-penalises rotation: a defender who plays 88 minutes in 25 of 38 games
    averages 58, falls under the cliff, and scores ZERO clean-sheet probability
    despite keeping clean sheets two games in three. Probability of starting and
    minutes given a start have to be modelled separately.
    """
    starts, mins = p["starts"], p["minutes"]
    if starts == 0:
        p_start = 0.15 if p["now_cost"] <= 45 else 0.35
        mins_if_start, p60_given_start = 70.0, 0.70
    else:
        p_start = min(1.0, starts / 38.0)
        mins_if_start = min(90.0, mins / starts)
        if mins_if_start >= 80:
            p60_given_start = 0.95
        elif mins_if_start >= 70:
            p60_given_start = 0.85
        elif mins_if_start >= 60:
            p60_given_start = 0.60
        else:
            p60_given_start = 0.25

    cop = p["chance_of_playing_next_round"]
    if cop is not None:
        p_start *= cop / 100.0
    if p["status"] != "a":
        p_start *= 0.5

    p_start = max(0.0, min(1.0, p_start))
    return p_start, mins_if_start, p_start * p60_given_start


def expected_minutes(p):
    """Average minutes per gameweek. Used for eligibility screens, not scoring."""
    p_start, mins_if_start, _ = minutes_profile(p)
    return p_start * mins_if_start


def defcon_probability(p, mins_if_start, priors):
    """Odds of clearing the threshold GIVEN a start. Caller scales by p_start."""
    pos = POS[p["element_type"]]
    thresh = DEFCON_THRESHOLD[pos]
    if thresh > 100 or mins_if_start <= 0:
        return 0.0
    rate90 = shrunk(p, "defensive_contribution_per_90", "dc90", priors)
    if rate90 <= 0:
        return 0.0
    return poisson_at_least(thresh, rate90 * mins_if_start / 90.0)


def clean_sheet_probability(p, fdr, priors):
    """Clean-sheet odds GIVEN 60+ minutes. Caller scales by p_60."""
    pos = POS[p["element_type"]]
    if CS_PTS[pos] == 0:
        return 0.0
    prior = priors.get(pos, {}).get("cs_rate", 0.25)
    own = p["clean_sheets"] / p["starts"] if p["starts"] else prior
    w = p["minutes"] / (p["minutes"] + SHRINK_K)
    base = w * own + (1 - w) * prior
    return max(0.0, min(0.85, base * CS_MULT.get(fdr, 1.0)))


def expected_points(p, fixtures, priors, ratings=None):
    """
    Expected points across the given fixtures (list of {'difficulty': n}).
    Returns (total, per_game, breakdown_dict).
    """
    pos = POS[p["element_type"]]
    p_start, mins_if_start, p_60 = minutes_profile(p)
    if not fixtures or p_start <= 0:
        return 0.0, 0.0, {}
    mins = p_start * mins_if_start

    starts = max(1, p["starts"])
    xg90 = shrunk(p, "expected_goals_per_90", "xg90", priors)
    xa90 = shrunk(p, "expected_assists_per_90", "xa90", priors)
    saves90 = shrunk(p, "saves_per_90", "saves90", priors)
    gc90 = shrunk(p, "goals_conceded_per_90", "gc90", priors)
    w = p["minutes"] / (p["minutes"] + SHRINK_K)
    bonus_prior = priors.get(pos, {}).get("bonus_rate", 0.0)
    own_bonus = p["bonus"] / starts if p["starts"] else bonus_prior
    bonus_per_start = w * own_bonus + (1 - w) * bonus_prior
    yellows_per_start = p["yellow_cards"] / starts if p["starts"] else 0.0

    tot = defaultdict(float)
    for fx in fixtures:
        fdr = fx.get("difficulty", 3)
        if ratings and "opp_id" in fx:
            am, cs_team, exp_ga = fixture_model(fx["team_id"], fx["opp_id"],
                                                fx["home"], ratings)
        else:
            am, cs_team, exp_ga = ATTACK_MULT.get(fdr, 1.0), None, 1.45
        share = mins / 90.0

        # 2 pts for 60+, 1 pt for any appearance short of that
        tot["appearance"] += p_60 * 2.0 + (p_start - p_60) * 1.0
        tot["goals"] += xg90 * share * am * GOAL_PTS[pos]
        tot["assists"] += xa90 * share * am * ASSIST_PTS

        # a clean sheet is a TEAM outcome: model it from expected goals against,
        # blended with the player's own history to catch role effects
        cs_hist = clean_sheet_probability(p, fdr, priors)
        cs = 0.75 * cs_team + 0.25 * cs_hist if cs_team is not None else cs_hist
        tot["clean_sheet"] += p_60 * cs * CS_PTS[pos]

        tot["defcon"] += p_start * defcon_probability(p, mins_if_start, priors) * DEFCON_PTS

        if pos == "GK":
            # saves are flat-rated in 2026/27: roughly 1 point per 3 saves
            tot["saves"] += (saves90 * share / 3.0)
        if pos in ("GK", "DEF"):
            # -1 per 2 conceded, worse against strong attacks
            tot["conceded"] -= (exp_ga * share) / 2.0

        tot["bonus"] += bonus_per_start * (mins / 90.0)
        tot["cards"] -= yellows_per_start * (mins / 90.0)

    total = sum(tot.values())
    return total, total / len(fixtures), dict(tot)


def build_fixtures(horizon=6, start_gw=None):
    boot = load("cache_bootstrap.json")
    fx = load("cache_fixtures.json")
    events = boot["events"]
    gw = start_gw or next((e["id"] for e in events if e.get("is_next")), 1)
    teams = {t["id"]: t for t in boot["teams"]}

    out = defaultdict(list)
    for x in sorted((y for y in fx if y["event"] and gw <= y["event"] < gw + horizon),
                    key=lambda y: y["event"]):
        out[x["team_h"]].append({"gw": x["event"], "opp": teams[x["team_a"]]["short_name"],
                                 "ha": "H", "difficulty": x["team_h_difficulty"],
                                 "team_id": x["team_h"], "opp_id": x["team_a"], "home": True})
        out[x["team_a"]].append({"gw": x["event"], "opp": teams[x["team_h"]]["short_name"],
                                 "ha": "A", "difficulty": x["team_a_difficulty"],
                                 "team_id": x["team_a"], "opp_id": x["team_h"], "home": False})
    return boot, dict(out), gw


def score_all(boot, fixtures, horizon):
    priors = positional_priors(boot)
    ratings = team_ratings(boot)
    rows = []
    for p in boot["elements"]:
        fx = fixtures.get(p["team"], [])[:horizon]
        total, per, brk = expected_points(p, fx, priors, ratings)
        rows.append({"p": p, "ep_total": total, "ep_game": per, "breakdown": brk,
                     "fixtures": fx})
    return rows


def owned_ids():
    """
    Who you ACTUALLY own, straight from the API.

    squad_ids.json is a name->id cache for the planned squad in
    squad_and_fixtures.json, and it accumulates every name ever looked up --
    it grew to 30 entries after transfers and is not a record of ownership.
    The live team is the only source of truth.
    """
    try:
        import fpl_write
        s = fpl_write.session()
        entry, _ = fpl_write.whoami(s)
        if entry:
            team = fpl_write.my_team(entry, s)
            ids = {pk["element"] for pk in team["picks"]}
            (STATE / "my_squad.json").write_text(
                json.dumps(sorted(ids)), encoding="utf-8")
            return ids
    except Exception:
        pass

    cached = STATE / "my_squad.json"
    if cached.exists():
        return set(json.loads(cached.read_text(encoding="utf-8")))

    f = STATE / "squad_ids.json"
    if f.exists():
        vals = list(json.loads(f.read_text(encoding="utf-8")).values())
        if len(vals) == 15:
            return set(vals)
    return set()


def fmt_row(r, teams):
    p = r["p"]
    run = " ".join(f"{x['opp']}{x['ha']}{x['difficulty']}" for x in r["fixtures"][:4])
    return (f"{p['web_name']:<17}{POS[p['element_type']]:<4}"
            f"{teams[p['team']]['short_name']:<5}{p['now_cost']/10:>5.1f}"
            f"{r['ep_game']:>7.2f}{r['ep_total']:>7.1f}"
            f"{r['breakdown'].get('defcon', 0)/max(1,len(r['fixtures'])):>7.2f}"
            f"{float(p['selected_by_percent']):>7.1f}  {run}")


HEAD = (f"{'PLAYER':<17}{'POS':<4}{'TEAM':<5}{'£':>5}{'EP/GW':>7}{'EPtot':>7}"
        f"{'DefC':>7}{'Own%':>7}  FIXTURES")


def main():
    ap = argparse.ArgumentParser(description="FPL expected-points model")
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--squad", action="store_true", help="score your 15")
    ap.add_argument("--pos", help="GK/DEF/MID/FWD")
    ap.add_argument("--max", type=float, help="max price")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--upgrades", action="store_true",
                    help="best same-position upgrade for each of your players")
    args = ap.parse_args()

    boot, fixtures, gw = build_fixtures(args.horizon)
    teams = {t["id"]: t for t in boot["teams"]}
    rows = score_all(boot, fixtures, args.horizon)
    by_id = {r["p"]["id"]: r for r in rows}
    mine = owned_ids()

    print(f"\nExpected points, GW{gw}-{gw+args.horizon-1} "
          f"({args.horizon} gameweeks)\n")

    if args.squad:
        print(HEAD)
        print("-" * 104)
        sq = sorted((by_id[i] for i in mine if i in by_id),
                    key=lambda r: (r["p"]["element_type"], -r["ep_game"]))
        for r in sq:
            print(fmt_row(r, teams))
        print("-" * 104)
        print(f"{'SQUAD TOTAL':<31}{sum(r['ep_total'] for r in sq):>19.1f} "
              f"expected points over {args.horizon} GWs")
        return

    if args.upgrades:
        print(f"{'OUT':<17}{'EP/GW':>7}   ->  {'IN':<17}{'EP/GW':>7}{'GAIN':>7}{'COST':>7}")
        print("-" * 78)
        for pid in sorted(mine, key=lambda i: by_id[i]["p"]["element_type"] if i in by_id else 9):
            if pid not in by_id:
                continue
            cur = by_id[pid]
            budget = cur["p"]["now_cost"]
            cands = [r for r in rows
                     if r["p"]["element_type"] == cur["p"]["element_type"]
                     and r["p"]["id"] not in mine
                     and r["p"]["status"] == "a"
                     and r["p"]["now_cost"] <= budget + 10]
            if not cands:
                continue
            best = max(cands, key=lambda r: r["ep_game"])
            gain = best["ep_game"] - cur["ep_game"]
            if gain <= 0.05:
                continue
            delta = (best["p"]["now_cost"] - budget) / 10
            print(f"{cur['p']['web_name']:<17}{cur['ep_game']:>7.2f}   ->  "
                  f"{best['p']['web_name']:<17}{best['ep_game']:>7.2f}"
                  f"{gain:>7.2f}{delta:>+7.1f}m")
        return

    cands = rows
    if args.pos:
        want = [k for k, v in POS.items() if v == args.pos.upper()]
        cands = [r for r in cands if r["p"]["element_type"] in want]
    if args.max:
        cands = [r for r in cands if r["p"]["now_cost"] <= args.max * 10]
    cands = [r for r in cands if r["p"]["status"] == "a"]
    cands.sort(key=lambda r: -r["ep_game"])

    print(HEAD)
    print("-" * 104)
    for r in cands[:args.top]:
        mark = "*" if r["p"]["id"] in mine else " "
        print(mark + fmt_row(r, teams)[1:] if False else mark + " " + fmt_row(r, teams))


if __name__ == "__main__":
    main()
