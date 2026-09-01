#!/usr/bin/env python3
"""
The judgment layer: an LLM reading what the model cannot.

The expected-points model sees `Tarkowski, DefCon 0.83`. It cannot see
"Moyes said in yesterday's press conference that he's being rested." FPL's API
carries `scout_news_link` on flagged players -- real club articles -- and this
layer reads them.

THE DESIGN RULE: THE JUDGE CAN VETO, NEVER ACT.

  It may:  flag a player as risky, downgrade confidence, block a transfer.
  It may NOT: initiate a transfer, pick a captain, change the squad.

The asymmetry is the whole point. A false veto ("don't start Tarkowski") costs
a few points. A false action ("sell Haaland, buy X") costs 4 points AND leaves
you with the wrong player for weeks. The statistical model was confidently
wrong once already -- it benched a 12.0m midfielder for 4.5m fodder. An LLM
will be confidently wrong too. Only one of those failure modes is cheap.

Every claim must cite a source. No citation, no claim -- an unsourced verdict
is treated as "ok" and discarded.

    python fpl_judge.py                # advisory report, changes nothing
    python fpl_judge.py --json         # machine-readable, for fpl_daily
    python fpl_judge.py --model X      # override the model

Needs ANTHROPIC_API_KEY, or an `ant auth login` profile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import anthropic
except ImportError:
    sys.exit("pip install anthropic")

import fpl_model as M

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
MODEL = "claude-opus-5"

# The judge's output shape. Constrained so a hallucinated free-text answer
# cannot be mistaken for a decision.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "overall": {
            "type": "string",
            "description": "One sentence: is there anything the manager must act on before the deadline?",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "low if you could not fetch or read the sources you needed.",
        },
        "players": {
            "type": "array",
            "description": "Only players where you found something the numbers do not show. Omit the rest.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["ok", "monitor", "bench", "urgent"],
                        "description": (
                            "bench = do not start this week. urgent = likely to miss entirely. "
                            "monitor = worth watching, no action. ok = explicitly fine."
                        ),
                    },
                    "reason": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": "URL you read this in, or the exact API news string. Never invent one.",
                    },
                },
                "required": ["name", "verdict", "reason", "source"],
                "additionalProperties": False,
            },
        },
        "blocked_transfers": {
            "type": "array",
            "description": "Players the engine should NOT transfer in, and why.",
            "items": {"type": "string"},
        },
    },
    "required": ["overall", "confidence", "players", "blocked_transfers"],
    "additionalProperties": False,
}

SYSTEM = """You are a judgment layer over a Fantasy Premier League expected-points model.

The model already handles arithmetic: expected points, clean-sheet odds, defensive
contribution thresholds, fixture difficulty. Do not second-guess its numbers — you
will be worse at that than it is.

Your job is the thing it cannot do: read. Press conferences, club announcements,
injury updates, rotation hints. Information that exists in prose and has not reached
the API's structured fields yet.

Hard rules:

1. You advise. You never decide. You cannot transfer, captain, or pick a squad.
   Your output changes the lineup only by flagging a player as unfit to start.
2. Every claim needs a source you actually read — a URL, or the exact news string
   from the API. If you cannot source it, do not say it. An unsourced verdict is
   discarded, so guessing gains you nothing and costs credibility.
3. Report only what the numbers do not already show. "Haaland has good fixtures"
   is not useful; the model knows. "Guardiola said Haaland is a doubt" is useful.
4. Silence is a valid answer. Most days there is nothing to report. Say so.
5. Prefer benching to selling. A 75% flag is a bench decision. Only call something
   urgent if the player is genuinely likely to miss the match entirely.
6. If you could not fetch the sources you needed, set confidence to low and say
   what you could not read. Do not fill the gap with plausible-sounding guesses."""


def build_context(horizon=6):
    """Everything the judge needs, assembled from the API."""
    boot, fixtures, gw = M.build_fixtures(horizon)
    rows = M.score_all(boot, fixtures, horizon)
    by = {r["p"]["id"]: r for r in rows}
    mine = M.owned_ids()
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}

    ev = [e for e in boot["events"] if e["id"] == gw][0]
    played = len([e for e in boot["events"] if e.get("finished")])

    squad, links = [], []
    for pid in mine:
        if pid not in by:
            continue
        r = by[pid]
        p = r["p"]
        entry = {
            "name": p["web_name"],
            "pos": M.POS[p["element_type"]],
            "club": teams[p["team"]],
            "price": p["now_cost"] / 10,
            "ep_per_gw": round(r["ep_game"], 2),
            "owned_by_pct": float(p["selected_by_percent"]),
            "status": p["status"],
            "chance_of_playing": p["chance_of_playing_next_round"],
            "api_news": p["news"] or None,
            "next_fixtures": [f"{f['opp']}({f['ha']}) FDR{f['difficulty']}"
                              for f in r["fixtures"][:4]],
        }
        if p.get("scout_news_link"):
            entry["news_url"] = p["scout_news_link"]
            links.append(p["scout_news_link"])
        squad.append(entry)

    # League-wide flags worth knowing about. Heavily-owned players are rarely
    # flagged (they are heavily owned BECAUSE they are fit), so a high ownership
    # threshold matches nobody. Surface anyone with a readable club article, plus
    # anyone with meaningful ownership, and let the judge decide what matters.
    league_news = []
    for p in boot["elements"]:
        if not p.get("news") or p["id"] in mine:
            continue
        cop = p["chance_of_playing_next_round"]
        own = float(p["selected_by_percent"])
        if p.get("scout_news_link") or own >= 1.0:
            league_news.append({
                "name": p["web_name"], "club": teams[p["team"]],
                "owned_by_pct": own, "chance_of_playing": cop,
                "news": p["news"], "news_url": p.get("scout_news_link"),
            })
    league_news.sort(key=lambda x: -x["owned_by_pct"])

    links += [n["news_url"] for n in league_news[:12] if n.get("news_url")]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gameweek": gw,
        "deadline": ev["deadline_time"],
        "gameweeks_played": played,
        "model_confidence_note": (
            f"Only {played} gameweek(s) played. The expected-points figures are "
            f"heavily shrunk toward positional averages and lean on price. Treat "
            f"them as directional."
        ),
        "my_squad": squad,
        "widely_owned_players_with_news": league_news[:25],
    }, links, gw


def judge(context, links, model=MODEL, effort="high"):
    client = anthropic.Anthropic()

    urls = "\n".join(f"- {u}" for u in links) if links else "(none)"
    prompt = f"""Here is my FPL squad, the model's expected-points figures, and the
news the API is carrying. The deadline is {context['deadline']}.

Read the club articles linked below and search for anything more recent — press
conferences, team news, injury updates — that affects these specific players.

Club articles the API linked to flagged players:
{urls}

Full context:
```json
{json.dumps(context, indent=2, ensure_ascii=False)}
```

Tell me only what the numbers do not already show. If nothing material has
happened, say so plainly — that is the expected answer most days."""

    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
        },
        tools=[
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
        ],
        messages=[{"role": "user", "content": prompt}],
    )

    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {resp.stop_details}")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("no text block in response")
    out = json.loads(text)
    out["_usage"] = {
        "input": resp.usage.input_tokens,
        "output": resp.usage.output_tokens,
        "model": resp.model,
    }
    return out


def sanitise(verdict):
    """
    Enforce the citation rule and the veto-only rule.

    A verdict with no source is downgraded to 'ok' — an unsourced claim is not
    allowed to change anything. This is the single most important guard here:
    it makes hallucinating strictly useless rather than dangerous.
    """
    kept, dropped = [], []
    for p in verdict.get("players", []):
        src = (p.get("source") or "").strip()
        if not src or src.lower() in {"none", "n/a", "unknown", "model data"}:
            dropped.append(p.get("name", "?"))
            continue
        if p.get("verdict") not in {"ok", "monitor", "bench", "urgent"}:
            dropped.append(p.get("name", "?"))
            continue
        kept.append(p)
    verdict["players"] = kept
    verdict["_dropped_unsourced"] = dropped
    return verdict


def save(verdict):
    STATE.mkdir(exist_ok=True)
    (STATE / "judge.json").write_text(
        json.dumps({"at": datetime.now(timezone.utc).isoformat(), **verdict},
                   indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="LLM judgment layer over the EP model")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default="high",
                    choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--context", action="store_true",
                    help="write the context file and print a prompt to paste into "
                         "Claude Code — costs nothing, uses your existing subscription")
    args = ap.parse_args()

    context, links, gw = build_context(args.horizon)

    # Free path: no API call. Dump the context and let Claude Code do the reading,
    # which is covered by the Claude subscription you already pay for. The judge
    # only matters once a week before a deadline, so automating it buys little.
    if args.context:
        STATE.mkdir(exist_ok=True)
        out = STATE / "judge_context.json"
        out.write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"context written: {out}  ({len(json.dumps(context)):,} chars)")
        print("readable club articles: " + str(len(links)))
        print("")
        print("Paste this into Claude Code:")
        print("-" * 68)
        print(f"""Read state/judge_context.json — my FPL squad, the model's expected
points, and the news FPL is carrying. GW{gw} deadline is {context['deadline']}.

Fetch the club articles linked in there and search for anything more recent
(press conferences, team news, injury updates) affecting these players.

Rules: cite a source for every claim — no source, don't say it. Report only
what the numbers don't already show. Silence is the right answer most days.
Recommend benching, never selling; you're advising, not deciding.""")
        print("-" * 68)
        return

    try:
        verdict = sanitise(judge(context, links, args.model, args.effort))
    except anthropic.AuthenticationError:
        sys.exit("No API credentials. Set ANTHROPIC_API_KEY or run: ant auth login")
    except Exception as e:
        sys.exit(f"judge failed: {type(e).__name__}: {e}")

    save(verdict)

    if args.json:
        print(json.dumps(verdict, indent=2, ensure_ascii=False))
        return

    icon = {"ok": "  ", "monitor": " ?", "bench": " !", "urgent": "!!"}
    print(f"\nJUDGE — GW{gw}   confidence: {verdict['confidence']}")
    print(f"  {verdict['overall']}\n")
    if verdict["players"]:
        for p in verdict["players"]:
            print(f"{icon.get(p['verdict'], '  ')} {p['name']:<16}{p['verdict']:<9}{p['reason']}")
            print(f"     source: {p['source'][:88]}")
    else:
        print("  Nothing the model has not already priced in.")
    if verdict["blocked_transfers"]:
        print("\n  Blocked from transfer in:")
        for b in verdict["blocked_transfers"]:
            print(f"    - {b}")
    if verdict["_dropped_unsourced"]:
        print(f"\n  discarded (no source): {', '.join(verdict['_dropped_unsourced'])}")
    u = verdict.get("_usage", {})
    print(f"\n  {u.get('model')} · {u.get('input', 0):,} in / {u.get('output', 0):,} out")


if __name__ == "__main__":
    main()
