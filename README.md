# FPL Auto-Manager

An expected-points model, an exact squad optimiser, and a daily job that can
actually change your Fantasy Premier League team — not just tell you what to do.

Runs on your machine. Your credentials never leave it.

---

## ⚠️ Read this before anything else

This tool **makes real changes to your FPL team**. It can change your lineup,
move your captain and make transfers without asking you at the time.

**FPL's terms restrict automated access, and managers have been banned for
automating team management.** That risk lands on *your* account. Nobody can
promise you it's safe. If losing your team would genuinely upset you, run it
in `--dry-run` mode, which only ever reports.

Setup stores an **OIDC refresh token** in `state/auth.json`. It's valid for
about 180 days, and anyone who gets it can read *and modify* your team until
you log out of Premier League everywhere. `state/` is gitignored — keep it
that way, and don't sync it to cloud storage.

---

## Why this exists

**Every public FPL library is broken.** They all authenticate against
`users.premierleague.com`, which no longer resolves. FPL migrated to Ping
Identity OIDC and nobody published the new flow. The
[auth section](#how-fpl-authentication-works-in-202627) below documents it.

---

## Quick start

```bash
git clone <your-repo-url> && cd fpl-auto-manager
pip install -r requirements.txt
python fpl_setup.py
```

`fpl_setup.py` walks you through connecting your account, then verifies it against
the live API.

Look before you leap:

```bash
python fpl_model.py --squad     # score your current 15
python fpl_optimise.py          # what the model would pick instead
python fpl_daily.py --auto      # what it WOULD do — changes nothing
```

When you're happy:

```bash
python fpl_daily.py --apply     # once, now
python run_scheduler.py         # forever: 08:00 / 20:00 / 23:30 local
```

### Docker

```bash
docker compose up -d            # set TZ in docker-compose.yml first
docker compose logs -f
```

One-off commands:

```bash
docker compose run --rm fpl python fpl_model.py --squad
```

### Kill switch

```bash
touch state/PAUSE               # alerts continue, all writes stop
```

Checked before authentication, so it works even if something else is broken.

---

## What it does

| Command | Purpose |
|---|---|
| `fpl_setup.py` | First-run wizard: consent, token, verification |
| `fpl_model.py` | Expected points per player from the 2026/27 scoring rules |
| `fpl_optimise.py` | Exact squad selection as an integer program |
| `fpl_daily.py` | Monitoring + the auto-action engine |
| `fpl_token.py` | OIDC refresh-token rotation |
| `fpl_write.py` | Authenticated API layer |
| `fpl_create.py` | Create a team from scratch |
| `fpl_tracker.py` | Live points, auto-subs, fixture ticker (no auth needed) |
| `run_scheduler.py` | Cross-platform scheduler |

### The model

Expected points built from the actual scoring rules, not a heuristic:

- **Clean sheets** — Poisson. `P(opponent scores 0)` from opponent attack, your
  defence and venue, rather than FPL's hand-set 1–5 difficulty rating.
- **Defensive contribution** — Poisson `P(actions ≥ threshold)`, 10 for
  defenders and 12 for midfielders. New for 2026/27 and widely under-priced.
- **Minutes** — `P(start)` and `minutes | start` modelled separately. Averaging
  them and then testing against the 60-minute threshold double-penalises
  rotation (see [Bugs worth knowing about](#bugs-worth-knowing-about)).
- **Shrinkage** — every per-90 rate is pulled toward the positional mean by
  sample size, so a player with 2 minutes and one shot doesn't outrank Haaland.
  `K = 250`, calibrated against last season's actual points.
- **Team ratings** — attack and defence derived from last season's aggregated xG.

Correlation with actual points per start, n=217 regular starters: **0.848**.
FPL's own `ep_next` projection over the same set: **0.323**.

That's a backtest on the season the ratings were built from, so some of the fit
is circular. Treat it as a sanity check, not proof.

### The optimiser

Squad selection as an integer program (PuLP/CBC): maximise expected points of
the starting XI subject to budget, formation, the 3-per-club cap, and captaincy
doubling. Exact, not greedy.

```bash
python fpl_optimise.py --lock Haaland --lock Salah   # keep players you believe in
python fpl_optimise.py --apply                       # only while transfers are free
```

Locking matters. The unconstrained optimum will happily sell a 70%-owned
premium to gain 0.3 points a week — which is a bad trade in a *rank* game.

### Safety rails

Hard-coded, not configurable:

| Rule | Why |
|---|---|
| Never take a points hit | A −4 needs to clear 4 points of EV. A script can't judge that. |
| Only sell at ≤25% chance of playing | A 75% flag is a bench decision, not a sell decision. |
| One auto-transfer per gameweek | Runaway protection. |
| Transfers need ≥1.5 expected points of gain | Stops churn on noise. |
| Captain only moves if unavailable | Captaincy is a judgement call, not maintenance. |
| Nothing after the deadline | No pointless writes into a live gameweek. |
| `state/PAUSE` halts everything | Kill switch. |

Every write is appended to `state/writes.log` with the exact payload and the
API's response.

---

## How FPL authentication works in 2026/27

The part that isn't documented anywhere else.

**What changed.** `users.premierleague.com` is decommissioned. The whole
`pl_profile` / `sessionid` / `csrftoken` cookie scheme is gone. Authentication
is now a Ping Identity OAuth bearer token, sent as a header:

```
x-api-authorization: Bearer <jwt>
```

No cookies are involved. Verified working from a plain script — no Cloudflare
or DataDome challenge on the API host.

**Token lifetime is 8 hours**, and access tokens can only be minted through a
browser login. There is no scripted password grant — the discovery document
lists `authorization_code`, `refresh_token`, `client_credentials` and
`device_code`, but not `password`.

**Rotation without a browser.** The discovery document advertises the
`refresh_token` grant and the token endpoint accepts scripted POSTs:

```
POST https://account.premierleague.com/as/token
grant_type=refresh_token & client_id=<spa client id> & refresh_token=<token>
```

Refresh tokens last ~180 days and **rotate on every exchange** — the response
carries a new one and the old one dies immediately. Persist the new one or the
chain breaks on the next run. Your browser also caches the dead copy, so
re-reading `localStorage` after a failed exchange hands you the same expired
token again; hard-reload the page first.

### Use a separate browser for the token. This matters more than anything else here.

Every token carries a `sid` -- the Ping **session** it belongs to. Refresh-token
rotation is per-token, but **revocation is per-session**: when a session
re-authenticates, every token issued under it dies at once.

So if you copy the refresh token out of the browser you actually play FPL in,
you are sharing a session with that browser. The next time you open the site,
its SPA re-auths, and your automation's token is revoked. It will look like the
tool broke on its own, days later, for no reason.

**Do this instead.** Log in to FPL in a browser you do not otherwise use --
a second browser, or a fresh profile -- and take the refresh token from there.
Then never browse FPL in it again.

```bash
python fpl_token.py --login    # drives its own isolated profile, and verifies
                               # the new sid differs from the old one
```

`--login` is the durable version: because the script owns that profile, it can
re-mint headlessly when the chain breaks, without you. Taking the token from a
second browser by hand also works and needs no Playwright -- you just lose the
self-healing.

To check which session you are on:

```bash
python -c "import fpl_token,fpl_write;print(fpl_write.decode_jwt(fpl_token._read()['token'])['sid'])"
```

If that matches the session in your everyday browser, it will break. If it
differs, your browsing cannot touch it.

**Endpoints**

| Endpoint | Auth | Returns |
|---|---|---|
| `bootstrap-static/` | no | every player, team and gameweek. ~4MB, cache it |
| `fixtures/` | no | all fixtures with difficulty ratings |
| `entry/{id}/` | no | name, region, bank, squad value |
| `entry/{id}/history/` | no | rank and points history |
| `entry/{id}/event/{gw}/picks/` | no | XI, captain, bench for a played gameweek |
| `event/{gw}/live/` | no | live points and BPS for every player |
| `me/` | **yes** | your profile and entry id |
| `my-team/{id}/` | **yes** | selling prices, bank, free transfers, chips |
| `my-team/{id}/` (POST) | **yes** | set lineup, captain, vice, bench order, chip |
| `transfers/` (POST) | **yes** | make transfers |
| `entry-create/` (POST) | **yes** | create a team |

Note how much is public. **A read-only advisory tool needs no authentication at
all** — just a team id.

**Payload shapes**

```jsonc
// POST /api/transfers/
{"confirmed": true, "entry": 1234567, "event": 2, "chip": null,
 "transfers": [{"element_in": 123, "element_out": 456,
                "purchase_price": 75, "selling_price": 80}]}

// POST /api/my-team/{id}/   — positions 1-11 are the XI, 12-15 the bench order
{"chip": null,
 "picks": [{"element": 1, "position": 1,
            "is_captain": false, "is_vice_captain": false}]}

// POST /api/entry-create/
{"email": false, "favourite_team": 14, "name": "Team Name",
 "terms_agreed": true,
 "picks": [{"element": 1, "purchase_price": 60}]}
```

Transfers must swap **like for like** — a defender can only be replaced by a
defender, or the whole request is rejected.

Prices are in tenths: `75` is £7.5m. `selling_price` must come from
`my-team/` — it is what *you* would receive, which differs from `now_cost`
after price rises.

---

## Bugs worth knowing about

Documented because each one silently produced plausible, wrong answers.

**1. Small-sample outliers.** A player with 2 minutes and one shot had an xG/90
of 3.6 and was ranked the best forward in the game. Fixed with empirical-Bayes
shrinkage toward positional means.

**2. The minutes cliff.** Expected minutes were `minutes-per-start × start-rate`.
A defender playing 88 minutes in 25 of 38 games came out at 58 "average"
minutes, fell under the 60-minute threshold, and scored **zero** clean-sheet
probability — despite keeping them two games in three. Averaging before
thresholding double-penalises rotation. Fixed by modelling `P(start)` and
`minutes | start` separately.

**3. Credential clobbering.** Saving a new access token rewrote the whole auth
file, deleting the refresh token that had just been rotated in — breaking the
chain permanently after exactly one refresh. The write now merges.

**4. A benchmarking error that nearly binned the model.** Initial benchmarking
said FPL's own projections beat this model, 0.323 to 0.212. The error was in the
benchmark: per-*gameweek* expected points were being compared against per-*start*
actuals, injecting start probability as pure noise. Corrected, the same model
scored 0.867.

**5. Session-scoped revocation.** The write half sat dead for six days without
anyone noticing. A refresh token taken from the everyday browser shared a Ping
session with it; the first visit to the FPL site revoked the whole session. The
report said `AUTO OFF` in a section nobody reads -- a silent failure in the one
component whose entire job is not failing silently. Fixed twice over: the token
now comes from an isolated session, and a broken chain raises a `RED` alert at
the top of the action list with a running count of consecutive failures.

Measure carefully. The wrong yardstick will make you throw away working code.

---

## Not included

**A hosted version.** Running this as a service would mean holding other
people's refresh tokens — long-lived credentials that can modify their teams —
and using FPL's first-party `client_id` at scale. A breach would compromise
real accounts, and the ban risk would land on your users. That's why this is
self-hosted only.

If you want to build something public, the **read-only** path is clean: every
endpoint needed for analysis and suggestions is public and needs nothing but a
team id.

---

## Licence

MIT. Not affiliated with, endorsed by, or connected to the Premier League.
Use at your own risk — see the warning at the top.
