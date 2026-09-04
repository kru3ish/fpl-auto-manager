#!/usr/bin/env python3
"""
What the engine actually did, what it decided against, and proof it ran.

A briefing that only reports the current squad cannot answer the two questions
an owner actually has: is this thing alive, and what has it been doing behind
my back? For a week it answered neither -- it applied a transfer and the next
morning's email still read "No action needed", because that sentence was
hardcoded.

Three sources, none of which require the engine to be re-run:

  applied()    -- state/writes.log. Every authenticated write, with its HTTP
                  status. This is the audit trail, not a summary of intent:
                  a rejected write is still recorded, so a transfer that FPL
                  refused shows up as refused rather than silently missing.

  rejected()   -- decisions considered and declined. The engine doing nothing
                  is a decision, and an owner cannot tell a working engine from
                  a stalled one unless it says what it looked at.

  heartbeat()  -- when the pipeline last ran, and whether auth is alive.
"""

from __future__ import annotations

import datetime
import io
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
WRITES = STATE / "writes.log"
BEAT = STATE / "heartbeat.json"

# A swap has to clear this over the horizon before it is worth a free transfer.
# Below it, the difference is inside the model's own noise band and banking the
# transfer is worth more than spending it.
TRANSFER_THRESHOLD_EP = 2.0


def _names(boot):
    return {e["id"]: e["web_name"] for e in boot["elements"]}


def _iter_writes():
    if not WRITES.exists():
        return
    for line in io.open(WRITES, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def applied(boot, days=21):
    """
    Every write the engine made in the window, newest first.

    Reads the audit log rather than reconstructing intent, so a write FPL
    rejected appears as rejected instead of vanishing.
    """
    nm = _names(boot)
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days))
    out = []
    prev_caps = {}

    for rec in _iter_writes():
        try:
            ts = datetime.datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        kind, ok = rec.get("kind"), 200 <= rec.get("status", 0) < 300
        pay = rec.get("payload") or {}

        if kind == "transfers":
            for mv in pay.get("transfers", []):
                out.append({
                    "ts": ts, "ok": ok, "type": "TRANSFER",
                    "what": f'{nm.get(mv["element_out"], mv["element_out"])} '
                            f'→ {nm.get(mv["element_in"], mv["element_in"])}',
                    "note": "" if ok else _why_rejected(rec.get("response", "")),
                })
        elif kind == "set_lineup":
            cap = next((p["element"] for p in pay.get("picks", []) if p.get("is_captain")), None)
            # Only report the armband when it actually moves. The engine writes
            # the full lineup on every intervention, so reporting each write
            # would show a "captain change" that changed nothing.
            if cap is not None and prev_caps.get("cap") not in (None, cap):
                out.append({"ts": ts, "ok": ok, "type": "CAPTAIN",
                            "what": f'{nm.get(prev_caps["cap"], "?")} → {nm.get(cap, cap)}',
                            "note": ""})
            if cap is not None:
                prev_caps["cap"] = cap
        elif kind == "entry_create":
            out.append({"ts": ts, "ok": ok, "type": "SQUAD",
                        "what": f'Team created: {pay.get("name", "?")}', "note": ""})

    out.sort(key=lambda r: r["ts"], reverse=True)
    return out


def _why_rejected(body):
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "rejected"
    msgs = []
    for item in (data.get("transfers") or []):
        for err in (item.get("non_field_errors") or []):
            m = err.get("message")
            if m and m not in msgs:
                msgs.append(m)
    return "; ".join(msgs)[:120] or "rejected"


def rejected(boot, rows, mine, horizon=6, free_transfers=None):
    """
    The best swaps available right now and why none were taken.

    Doing nothing is a decision. Reporting only the squad makes a healthy engine
    and a dead one look identical from the outside, so this reports what was on
    the table and what the bar was.
    """
    import fpl_auto

    by = {r["p"]["id"]: r for r in rows}
    prices = {e["id"]: e for e in boot["elements"]}
    squad = [by[i] for i in mine if i in by]
    owned = set(mine)

    # Selling price is not list price -- a player bought before a rise sells for
    # less than he now costs, and using now_cost silently inflates every budget.
    # This used to fail open into that wrong answer, so it now reports the
    # breakage instead of quietly costing accuracy.
    tr_raw = {}
    bank, sell, entry = 0, {}, json.loads(
        (STATE / "config.json").read_text(encoding="utf-8"))["team_id"]
    try:
        import fpl_write
        team = fpl_write.my_team(entry)
        tr_raw = team["transfers"]
        bank = team["transfers"]["bank"]
        sell = {p["element"]: p["selling_price"] for p in team["picks"]}
        if free_transfers is None:
            free_transfers = max(0, team["transfers"].get("limit") or 0)
    except Exception as exc:
        print(f"  [activity] live team unavailable ({exc}); using list prices")
        if free_transfers is None:
            free_transfers = 0

    club_count = {}
    for r in squad:
        club_count[r["p"]["team"]] = club_count.get(r["p"]["team"], 0) + 1

    cands = []
    for out_r in squad:
        op = out_r["p"]
        budget = bank + sell.get(op["id"], op["now_cost"])
        for in_r in rows:
            ip = in_r["p"]
            if (ip["id"] in owned or ip["element_type"] != op["element_type"]
                    or ip["status"] != "a" or ip["now_cost"] > budget):
                continue
            # Max three per club, counting the outgoing player leaving.
            n = club_count.get(ip["team"], 0) - (1 if ip["team"] == op["team"] else 0)
            if n >= 3:
                continue
            gain = in_r["ep_total"] - out_r["ep_total"]
            if gain > 0:
                cands.append({
                    "out": op["web_name"], "in": ip["web_name"],
                    "out_id": op["id"], "in_id": ip["id"],
                    "key": f'{op["id"]}>{ip["id"]}',
                    "label": f'{op["web_name"]} -> {ip["web_name"]}',
                    "gain": gain, "cost": (ip["now_cost"] - sell.get(op["id"], op["now_cost"])) / 10,
                })
    cands.sort(key=lambda c: -c["gain"])

    # A proposal the owner already said no to should not reappear every morning.
    try:
        import fpl_inbox
        blocked = set(fpl_inbox.vetoed())
        cands = [c for c in cands if c["key"] not in blocked]
    except Exception:
        blocked = set()

    best = cands[0]["gain"] if cands else 0.0
    conf = fpl_auto.model_confidence()
    if not cands:
        why = "No affordable same-position upgrade exists."
    elif best < TRANSFER_THRESHOLD_EP:
        why = (f"Best available swap gains {best:.1f} EP over {horizon} GWs, under the "
               f"{TRANSFER_THRESHOLD_EP:.1f} EP bar. Inside the model's own noise band, "
               f"so the transfer is worth more banked.")
    elif conf < 0.5:
        why = (f"Best swap gains {best:.1f} EP, but model confidence is {int(conf * 100)}%. "
               f"Not acting on a ranking that is still mostly prior.")
    elif not free_transfers:
        why = f"Best swap gains {best:.1f} EP but there is no free transfer to spend."
    else:
        why = f"Best swap gains {best:.1f} EP and clears the bar — expect it to fire."

    # Best upgrade available for each individual player, so the squad table can
    # say why each name survived rather than only what the top three swaps were.
    per = {}
    for c in cands:
        if c["gain"] > per.get(c["out_id"], (0, ""))[0]:
            per[c["out_id"]] = (c["gain"], c["in"])

    return {"top": cands[:3], "why": why, "threshold": TRANSFER_THRESHOLD_EP,
            "per_player": per, "confidence": conf, "transfers": tr_raw,
            "free_transfers": free_transfers, "bank": bank / 10,
            "vetoed": len(blocked)}


def beat():
    """Record that the pipeline ran. Keeps a rolling window, newest last."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    hist = []
    if BEAT.exists():
        try:
            hist = json.loads(BEAT.read_text(encoding="utf-8"))
        except ValueError:
            hist = []
    hist.append(now)
    STATE.mkdir(exist_ok=True)
    BEAT.write_text(json.dumps(hist[-40:]), encoding="utf-8")


def heartbeat():
    """Recent run times and auth state -- the 'is it alive' panel."""
    runs = []
    if BEAT.exists():
        try:
            runs = json.loads(BEAT.read_text(encoding="utf-8"))
        except ValueError:
            runs = []
    parsed = []
    for r in runs[-6:]:
        try:
            parsed.append(datetime.datetime.fromisoformat(r))
        except ValueError:
            pass

    try:
        import fpl_write as W
        hours = W.token_hours_left()
    except Exception:
        hours = None

    return {"runs": sorted(parsed, reverse=True), "token_hours": hours,
            "reports": len(list((ROOT / "reports").glob("*.md")))
            if (ROOT / "reports").is_dir() else 0}


if __name__ == "__main__":
    import fpl_model as M

    boot, fx, gw = M.build_fixtures(6)
    rows = M.score_all(boot, fx, 6)
    mine = M.owned_ids()

    print("\nAPPLIED (last 21 days)")
    for a in applied(boot) or []:
        mark = "ok " if a["ok"] else "REJ"
        print(f"  {a['ts']:%d %b %H:%M}  {mark} {a['type']:<9} {a['what']} {a['note']}")

    r = rejected(boot, rows, mine)
    print("\nCONSIDERED, NOT TAKEN")
    for c in r["top"]:
        print(f"  {c['out']} → {c['in']:<16} +{c['gain']:.1f} EP  ({c['cost']:+.1f}m)")
    print(f"  reason: {r['why']}")

    h = heartbeat()
    print("\nPIPELINE")
    for t in h["runs"]:
        print(f"  ran {t:%d %b %H:%M} UTC")
    print(f"  token {h['token_hours']:.1f}h left | {h['reports']} reports on disk")
