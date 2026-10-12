# FPL auto-manager

Autonomous Fantasy Premier League manager for entry **4330346** (*DefCon Enjoyers*).
Scores every player, picks transfers and lineups, reads team news, writes the
changes to the real team through the authenticated FPL API, and emails a daily
briefing the owner can reply to.

Python 3.12, stdlib-first. Only `requests` and `pulp` are hard dependencies.

## Commands

```bash
python fpl_daily.py                 # report only, no writes
python fpl_daily.py --auto          # run the engine, dry run
python fpl_daily.py --apply         # run the engine and WRITE to the real team
python fpl_judge.py --via-claude-code --soft   # news layer, on the Claude subscription
python fpl_inbox.py --apply         # execute owner commands from email
python fpl_email.py --send          # send the briefing
python fpl_rank.py --captain        # captaincy by expected RANK gain
python fpl_flow.py                  # what 11M other managers are doing
python watchdog.py                  # off-machine check (also runs in Actions)
python fpl_token.py --status        # auth health
python test_flagged_starter.py      # regression tests
```

`run_daily.bat` (3x daily) and `run_inbox.bat` (every 20 min) are driven by
Windows Task Scheduler as **FPL Daily Check** and **FPL Owner Commands**.

## Layout

| file | role |
|---|---|
| `fpl_model.py` | expected points. Poisson, empirical-Bayes shrinkage, minutes model |
| `fpl_auto.py` | decision engine. Lineup, captaincy, transfers. Owns the write guards |
| `fpl_write.py` | authenticated API layer. Every write goes through here and is audited |
| `fpl_token.py` | OIDC refresh. Access tokens live **1 hour**; refresh tokens rotate |
| `fpl_daily.py` | watchdog + report. Alerts, fixture congestion, price moves |
| `fpl_judge.py` | LLM news layer. **Veto-only** |
| `fpl_inbox.py` | owner commands by email reply (`DO 1` / `NO 1` / `HOLD`) |
| `fpl_email.py` | HTML briefing + SMTP |
| `fpl_rank.py` | effective ownership, expected rank gain |
| `fpl_flow.py` | transfer-market flow — crowd behaviour, not opinion |
| `fpl_activity.py` | what the engine did and declined to do |
| `fpl_optimise.py` | PuLP integer program for squad building |
| `watchdog.py` | off-machine check. Public API only, no credentials |

`state/` is gitignored and holds live credentials. Never commit it, never print
its contents.

## Invariants — do not break these

Each one exists because the opposite shipped and cost something real.

**The judge can veto, never act.** It may flag a player, downgrade confidence or
block a transfer. It may not initiate a transfer, pick a captain or change the
squad. A verdict with no source is discarded; a verdict older than 3 days is
ignored. Worst case for a wrong verdict is a benching, never a purchase.

**Minimal intervention on the lineup.** The engine only reshuffles when someone
genuinely cannot play, or when a flagged starter loses on expected value. Left
to re-rank freely on thin data it once benched a £12.0m midfielder for £4.5m
fodder and made the fodder vice-captain.

**Captaincy only moves if the captain is unavailable.** Not on form, not on
fixtures. It is the owner's call.

**A flagged starter is usually worth keeping.** Auto-subs make him a free
option: his upside if he plays, the bench player's points if he does not. Bench
only when `ep_bench × cop/100 > ep_flagged`. Fixing a flag check without this
will bench players it should not.

**Never let a ratio with a tiny denominator rank anything.** Two clean sheets
made three Hull defenders look elite; 1,860 transfers on a 0.0%-owned keeper
scored +186,000% momentum. Absolute floor first, ratio second. `effective_k()`
scales shrinkage by matches played for the same reason.

**Chance-of-playing is the doubt already.** Do not multiply by it *and* halve
again for the status that produced it. That double-count put a £7.7m forward
below £4.5m bench fodder.

**Token refresh must hold the lock.** Two schedules refresh, and a refresh
rotates the refresh token. Concurrent refreshes leave the loser holding a
consumed token and auth dies silently. See `_refresh_lock`.

**Report what was decided, including "nothing".** A hardcoded "no action needed"
once printed for days after the engine had made a transfer. An engine that
cannot be distinguished from a dead one is indistinguishable from a dead one.

**Email commands: parse only the first unquoted line.** Replies quote the
briefing, and the briefing lists the command words. Scanning the whole body
executes instructions read back out of the quote. Only the configured sender,
only numbered options from the frozen manifest, re-validated before execution.

## Known gaps

- **Chips are never fired by code.** Wildcard, Triple Captain, Bench Boost and
  Free Hit remain manual on the FPL site. Deliberate.
- The model optimises points; `fpl_rank.py` reasons about rank but does not yet
  drive transfers.
- Horizon totals are compared where a single gameweek is meant, so a one-week
  flag is spread over six. Direction holds, magnitude does not.
- A powered-off machine still stops everything local. The Actions watchdog is
  the only thing that survives it.
