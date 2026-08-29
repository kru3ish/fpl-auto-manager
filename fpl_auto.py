#!/usr/bin/env python3
"""
Auto-action engine. Decides what to change, then (with --apply) changes it.

Operating rules, hard-coded and not overridable by config:

  1. NEVER take a points hit. If it costs 4, it doesn't happen.
  2. Only transfer out a player at 25% chance-of-playing or below. A 75% flag is
     a bench decision, not a sell decision.
  3. At most ONE auto-transfer per gameweek. Runaway protection.
  4. Never touch anything after the deadline has passed for the pending gameweek.
  5. Lineup changes (XI/bench/captain) are free and reversible, so they apply
     freely. Transfers are permanent, so they get every check above.
  6. If state/PAUSE exists, nothing is applied. Touch that file to kill it.

Everything is dry-run unless you pass --apply.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import fpl_write
import fpl_model as M

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
PAUSE = STATE / "PAUSE"
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

SELL_THRESHOLD = 25       # CoP at or below this is a sell candidate
BENCH_THRESHOLD = 75      # CoP below this should not start
MIN_FORMATION = {"GK": 1, "DEF": 3, "FWD": 1}
MIN_TRANSFER_GAIN = 1.5   # expected points over the horizon, else hold
PRICE_FALL_GUARD = -60000 # net transfers that signal an imminent price drop
MAX_PER_CLUB = 3


def paused() -> bool:
    return PAUSE.exists()


def _cop(p):
    """Chance of playing, normalised so 'no news' == 100."""
    v = p.get("chance_of_playing_next_round")
    return 100 if v is None else v


def model_confidence():
    """
    0..1 — how much the expected-points model has actually learned this season.

    Every per-90 rate shrinks toward the positional prior, so after one or two
    gameweeks EVERY player scores about the same and the ranking is noise. Price
    is the market's aggregated judgement and is a far better signal that early,
    so we blend toward it when the model has nothing to say.
    """
    try:
        boot = M.load("cache_bootstrap.json")
        played = sum(1 for e in boot["events"] if e.get("finished"))
    except Exception:
        return 1.0
    if played == 0:          # pre-season: bootstrap carries last season's totals
        return 1.0
    return max(0.0, min(1.0, played / 8.0))


def _rank_score(p, ep, conf):
    """Blend modelled expected points with price, weighted by model confidence."""
    price_proxy = (p["now_cost"] / 10.0) * 2.5     # ~same scale as 6-GW EP
    return conf * ep + (1 - conf) * price_proxy


# ----------------------------------------------------------------- lineup calc
def plan_lineup(team, elements_by_id, fixtures_by_team, ep_by_id):
    """
    Build the best legal XI from the 15 you already own.
    Returns (picks_payload, changes[]) or (None, []) if nothing needs changing.
    """
    conf = model_confidence()
    squad = []
    for pk in team["picks"]:
        p = elements_by_id[pk["element"]]
        cop = _cop(p)
        fx = fixtures_by_team.get(p["team"], [])
        has_fixture = bool(fx)
        fdr = fx[0]["difficulty"] if fx else 5

        # modelled expected points, blended toward price while the model is thin
        score = _rank_score(p, ep_by_id.get(p["id"], 0.0), conf)
        if not has_fixture:
            score -= 50                      # blank gameweek: bench him
        if cop <= SELL_THRESHOLD:
            score -= 100                     # out: never start
        elif cop < BENCH_THRESHOLD:
            score -= 20                      # doubtful: strongly prefer benching

        squad.append({
            "element": p["id"], "pos": POS[p["element_type"]], "score": score,
            "cop": cop, "name": p["web_name"], "was_start": pk["position"] <= 11,
            "was_pos": pk["position"], "playable": cop > SELL_THRESHOLD and has_fixture,
        })

    # MINIMAL INTERVENTION.
    #
    # The engine's job is to fix problems, not to re-optimise a working XI every
    # day on noisy data. Early in a season the model has almost no discrimination
    # -- every per-90 rate shrinks to the same positional prior -- and left to
    # rank freely it once benched a 12.0m midfielder in favour of 4.5m fodder and
    # made that fodder vice-captain. A lineup change is only justified when
    # somebody in the XI genuinely cannot play.
    unplayable = [x for x in squad if x["was_start"] and not x["playable"]]
    if not unplayable:
        return None, []

    gks = sorted([s for s in squad if s["pos"] == "GK"], key=lambda s: -s["score"])
    outs = sorted([s for s in squad if s["pos"] != "GK"], key=lambda s: -s["score"])
    # keep whoever is already starting and fine, ahead of anyone on the bench
    for lst in (gks, outs):
        lst.sort(key=lambda s: (not (s["was_start"] and s["playable"]), -s["score"]))

    xi = [gks[0]]
    counts = defaultdict(int, {"GK": 1})
    # guarantee the minimum shape first, from the best available in each slot
    for pos, need in (("DEF", 3), ("FWD", 1)):
        for s in [x for x in outs if x["pos"] == pos][:need]:
            xi.append(s)
            counts[pos] += 1
    # fill the remaining slots with the best of whatever is left
    chosen = {s["element"] for s in xi}
    for s in outs:
        if len(xi) == 11:
            break
        if s["element"] in chosen:
            continue
        xi.append(s)
        chosen.add(s["element"])
        counts[s["pos"]] += 1

    bench_out = sorted([s for s in outs if s["element"] not in chosen],
                       key=lambda s: -s["score"])
    bench = [gks[1]] + bench_out if len(gks) > 1 else bench_out

    xi.sort(key=lambda s: ({"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}[s["pos"]], -s["score"]))

    # Captaincy is a judgement call, not a maintenance task. Keep whoever you
    # chose unless they literally cannot play -- the engine's job is to stop the
    # armband dying on a ruled-out player, not to out-guess your captain pick.
    old_cap = next((pk["element"] for pk in team["picks"] if pk["is_captain"]), None)
    old_vice = next((pk["element"] for pk in team["picks"] if pk["is_vice_captain"]), None)
    by_id = {s["element"]: s for s in xi}

    playable = [s for s in xi if s["playable"]]
    ranked = sorted(playable or xi, key=lambda s: -s["score"])

    cap = by_id.get(old_cap)
    if cap is None or not cap["playable"]:
        cap = ranked[0]

    vice = by_id.get(old_vice)
    if vice is None or not vice["playable"] or vice["element"] == cap["element"]:
        vice = next((s for s in ranked if s["element"] != cap["element"]), ranked[0])

    picks, changes = [], []
    for i, s in enumerate(xi, start=1):
        picks.append({"element": s["element"], "position": i,
                      "is_captain": s["element"] == cap["element"],
                      "is_vice_captain": s["element"] == vice["element"]})
        if not s["was_start"]:
            changes.append(f"START {s['name']} (was benched)")
    for i, s in enumerate(bench, start=12):
        picks.append({"element": s["element"], "position": i,
                      "is_captain": False, "is_vice_captain": False})
        if s["was_start"]:
            if s["cop"] <= SELL_THRESHOLD:
                reason = " (ruled out)"
            elif s["cop"] < 100:
                reason = f" ({s['cop']}% flagged)"
            else:
                reason = ""
            changes.append(f"BENCH {s['name']}{reason}")

    if old_cap != cap["element"]:
        changes.append(f"CAPTAIN -> {cap['name']} (previous captain unavailable)")
    if old_vice != vice["element"]:
        changes.append(f"VICE -> {vice['name']}")

    return picks, changes


# --------------------------------------------------------------- transfer calc
def plan_transfer(team, elements_by_id, teams_by_id, fixtures_by_team, gw, ep_by_id):
    """
    One zero-hit transfer, only to replace someone who is genuinely out.
    Returns (move, explanation) or (None, reason_it_did_nothing).
    """
    tr = team["transfers"]
    # limit is None before your first deadline and on a wildcard: transfers are free
    unlimited = tr["limit"] is None or tr.get("status") == "unlimited"
    free = 99 if unlimited else tr["limit"] - tr["made"]
    if free < 1:
        return None, f"no free transfers left ({tr['made']}/{tr['limit']} used)"

    done = json.loads((STATE / "auto_transfers.json").read_text(encoding="utf-8")) \
        if (STATE / "auto_transfers.json").exists() else {}
    if done.get(str(gw)):
        return None, f"already auto-transferred once in GW{gw}"

    bank = tr["bank"]
    owned = {pk["element"] for pk in team["picks"]}
    sell_price = {pk["element"]: pk["selling_price"] for pk in team["picks"]}

    club_count = defaultdict(int)
    for pid in owned:
        club_count[elements_by_id[pid]["team"]] += 1

    casualties = []
    for pk in team["picks"]:
        p = elements_by_id[pk["element"]]
        if _cop(p) <= SELL_THRESHOLD:
            casualties.append((pk, p))
    if not casualties:
        return None, "nobody in the squad is ruled out"

    # worst first: prioritise a starter who is definitively out
    casualties.sort(key=lambda cp: (_cop(cp[1]), cp[0]["position"]))
    pk_out, p_out = casualties[0]
    budget = bank + sell_price[pk_out["element"]]

    best, best_score = None, float("-inf")
    for p in elements_by_id.values():
        if p["id"] in owned or p["element_type"] != p_out["element_type"]:
            continue
        if p["now_cost"] > budget or _cop(p) < 100:
            continue
        if p["status"] != "a":
            continue
        new_count = club_count[p["team"]] + (0 if p["team"] == p_out["team"] else 1)
        if new_count > MAX_PER_CLUB:
            continue
        if not fixtures_by_team.get(p["team"]):
            continue
        score = _rank_score(p, ep_by_id.get(p["id"], 0.0), model_confidence())
        if score > best_score:
            best, best_score = p, score

    if not best:
        return None, (f"{p_out['web_name']} is out but nothing affordable "
                      f"(budget GBP {budget/10:.1f}m) passes the filters")

    gain = best_score - ep_by_id.get(p_out["id"], 0.0)
    if gain < MIN_TRANSFER_GAIN:
        return None, (f"{p_out['web_name']} is out, but the best replacement "
                      f"({best['web_name']}) only gains {gain:.2f} expected points "
                      f"- under the {MIN_TRANSFER_GAIN} threshold. Holding.")

    move = {
        "element_in": best["id"],
        "element_out": p_out["id"],
        "purchase_price": best["now_cost"],
        "selling_price": sell_price[pk_out["element"]],
    }
    why = (f"{p_out['web_name']} ({teams_by_id[p_out['team']]['short_name']}, "
           f"{_cop(p_out)}% - {p_out['news'] or 'out'}) "
           f"-> {best['web_name']} ({teams_by_id[best['team']]['short_name']}, "
           f"GBP {best['now_cost']/10:.1f}m, form {best['form']}) "
           f"| free transfer, 0 hit, GBP {(budget - best['now_cost'])/10:.1f}m left in bank")
    return move, why


def record_transfer(gw):
    f = STATE / "auto_transfers.json"
    done = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    done[str(gw)] = datetime.now(timezone.utc).isoformat()
    f.write_text(json.dumps(done, indent=2), encoding="utf-8")


# --------------------------------------------------------------------- driver
def run(elements_by_id, teams_by_id, fixtures_by_team, gw, deadline, apply=False):
    """Returns a list of report lines."""
    out = []
    if paused():
        return ["**AUTO PAUSED** — `state/PAUSE` exists. Delete it to re-enable."]

    try:
        s = fpl_write.session()
        entry, player = fpl_write.whoami(s)
        team = fpl_write.my_team(entry, s)
    except fpl_write.NotAuthed as e:
        return [f"**AUTO OFF** — {e}"]
    except Exception as e:
        return [f"**AUTO ERROR** — {type(e).__name__}: {e}"]

    mode = "APPLY" if apply else "DRY RUN"
    hrs = fpl_write.token_hours_left()
    out.append(f"Logged in as {player.get('first_name')} {player.get('last_name')} "
               f"(entry {entry}) — **{mode}**")
    if hrs < 2:
        out.append(f"**Token expires in {hrs:.1f}h** — re-run "
                   f"`python fpl_write.py --set-token` or the next run does nothing.")
    else:
        out.append(f"Token valid for another {hrs:.1f}h.")

    if datetime.now(timezone.utc) >= deadline:
        out.append(f"Deadline for GW{gw} has passed — no changes made.")
        return out

    boot, fx_map, model_gw = M.build_fixtures(6)
    ep_by_id = {r["p"]["id"]: r["ep_total"] for r in M.score_all(boot, fx_map, 6)}

    tr = team["transfers"]
    free = "unlimited" if tr["limit"] is None else tr["limit"] - tr["made"]
    out.append(f"Bank GBP {tr['bank']/10:.1f}m · value GBP {tr['value']/10:.1f}m · "
               f"free transfers {free}")

    # ---- transfers first, so the lineup is built from the post-transfer squad
    move, why = plan_transfer(team, elements_by_id, teams_by_id, fixtures_by_team, gw, ep_by_id)
    if move:
        out.append(f"**TRANSFER**: {why}")
        if apply:
            try:
                fpl_write.make_transfers(entry, gw, [move], s=s)
                record_transfer(gw)
                out.append("  -> applied.")
                team = fpl_write.my_team(entry, s)
            except Exception as e:
                out.append(f"  -> REJECTED: {e}")
        else:
            out.append("  -> not sent (dry run). Pass --apply to execute.")
    else:
        out.append(f"No transfer: {why}")

    # ---- lineup
    picks, changes = plan_lineup(team, elements_by_id, fixtures_by_team, ep_by_id)
    if picks is None:
        out.append("Lineup untouched — everyone in the XI can play "
                   "(the engine only intervenes when someone genuinely can't).")
        return out
    if not changes:
        out.append("Lineup already optimal — no changes.")
        return out

    out.append("**LINEUP**: " + "; ".join(changes))
    if apply:
        try:
            fpl_write.set_lineup(entry, picks, s=s)
            out.append("  -> applied.")
        except Exception as e:
            out.append(f"  -> REJECTED: {e}")
    else:
        out.append("  -> not sent (dry run). Pass --apply to execute.")
    return out
