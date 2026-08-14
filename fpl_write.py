#!/usr/bin/env python3
"""
Authenticated layer for the FPL API (2026/27 auth).

FPL no longer uses session cookies. users.premierleague.com is decommissioned and
the old pl_profile / sessionid / csrftoken scheme is gone. Auth is now a Ping
Identity (account.premierleague.com) OAuth bearer token, sent as:

    x-api-authorization: Bearer <jwt>

No cookies are required at all -- verified working from a plain script with no
Cloudflare or DataDome challenge.

THE CATCH: these tokens live exactly 8 hours and can only be minted by logging in
through a real browser. There is no scripted login. So anything unattended will
stop working within 8 hours of the last time you pasted a token.

HOW TO GET A TOKEN (30 seconds):
  1. Log in at https://fantasy.premierleague.com
  2. F12 -> Network -> type  api/  in the Filter box
  3. Ctrl+R to reload, then click the  me/  row
  4. Right-click it -> Copy -> Copy as cURL
  5. python fpl_write.py --set-token     and paste the whole thing

     (Pasting just the bearer token itself also works.)

state/auth.json holds a live credential. Treat it like a password.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

BASE = "https://fantasy.premierleague.com/api"
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
AUTH_FILE = STATE / "auth.json"
AUDIT = STATE / "writes.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


class NotAuthed(Exception):
    pass


class TokenExpired(NotAuthed):
    pass


class WriteRejected(Exception):
    pass


# ------------------------------------------------------------------ token bits
def decode_jwt(tok: str) -> dict:
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def extract_token(raw: str) -> str:
    """Accept a bare JWT, an 'x-api-authorization: Bearer x' line, or a cURL paste."""
    raw = raw.strip()
    m = re.search(r"x-api-authorization:\s*Bearer\s+([A-Za-z0-9._\-]+)", raw, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\bBearer\s+([A-Za-z0-9._\-]+)", raw, re.I)
    if m:
        return m.group(1)
    # a bare JWT: three dot-separated base64url chunks
    m = re.search(r"\b(eyJ[A-Za-z0-9._\-]{40,})\b", raw)
    if m:
        return m.group(1)
    return ""


def save_token(raw: str):
    tok = extract_token(raw)
    if not tok:
        print("REJECTED - no bearer token found in that paste.")
        print("\n  I need the  x-api-authorization: Bearer ...  header from a")
        print("  fantasy.premierleague.com/api/ request. Cookies are NOT used")
        print("  by FPL any more - pasting a Cookie header will not work.")
        sys.exit(1)

    claims = decode_jwt(tok)
    if not claims:
        sys.exit("That token isn't a readable JWT.")
    iss = claims.get("iss", "")
    if "premierleague.com" not in iss:
        print(f"REJECTED - token issuer is '{iss}', not Premier League.")
        sys.exit(1)

    exp = claims.get("exp")
    now = datetime.now(timezone.utc).timestamp()
    if exp and exp <= now:
        mins = (now - exp) / 60
        sys.exit(f"That token expired {mins:.0f} minutes ago. Reload FPL and copy a fresh one.")

    STATE.mkdir(exist_ok=True)
    # MERGE, never replace: auth.json also holds the refresh token, and blowing
    # that away breaks the rotation chain permanently.
    existing = {}
    if AUTH_FILE.exists():
        try:
            existing = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    existing.update({"token": tok, "exp": exp})
    AUTH_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    try:
        AUTH_FILE.chmod(0o600)
    except OSError:
        pass

    left = (exp - now) / 3600 if exp else 0
    print(f"token saved to {AUTH_FILE}")
    print(f"  subject : {claims.get('sub')}")
    print(f"  expires : {datetime.fromtimestamp(exp, timezone.utc)}  "
          f"({left:.1f}h from now)")


def load_token() -> str:
    if not AUTH_FILE.exists():
        raise NotAuthed("No token stored. Run: python fpl_write.py --set-token")
    d = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    tok, exp = d.get("token"), d.get("exp")
    if not tok:
        raise NotAuthed("auth.json has no token. Re-run --set-token.")
    if exp and exp <= datetime.now(timezone.utc).timestamp():
        age = (datetime.now(timezone.utc).timestamp() - exp) / 3600
        raise TokenExpired(
            f"Token expired {age:.1f}h ago (they last 8h). "
            f"Reload FPL in your browser and re-run --set-token.")
    return tok


def token_hours_left() -> float:
    if not AUTH_FILE.exists():
        return 0.0
    exp = json.loads(AUTH_FILE.read_text(encoding="utf-8")).get("exp")
    if not exp:
        return 0.0
    return max(0.0, (exp - datetime.now(timezone.utc).timestamp()) / 3600)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "x-api-authorization": f"Bearer {load_token()}",
        "x-api-language": "en",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://fantasy.premierleague.com",
        "Referer": "https://fantasy.premierleague.com/",
    })
    return s


# --------------------------------------------------------------- authed reads
def whoami(s=None):
    """Verify the token and return (entry_id_or_None, player_dict)."""
    s = s or session()
    r = s.get(f"{BASE}/me/", timeout=25)
    if r.status_code in (401, 403):
        raise NotAuthed("Token rejected or expired. Re-run --set-token.")
    r.raise_for_status()
    player = r.json().get("player")
    if not player:
        raise NotAuthed("Token is not logged in (api/me returned no player).")
    return player.get("entry"), player


def my_team(entry, s=None):
    """Your live squad with selling prices, bank, free transfers, chip state."""
    if not entry:
        raise NotAuthed("No team yet - you haven't created a squad on the FPL site.")
    s = s or session()
    r = s.get(f"{BASE}/my-team/{entry}/", timeout=25)
    if r.status_code in (401, 403):
        raise NotAuthed("Token rejected on my-team. Re-run --set-token.")
    r.raise_for_status()
    return r.json()


# -------------------------------------------------------------------- writes
def _audit(kind, payload, status, body):
    STATE.mkdir(exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind, "payload": payload,
            "status": status, "response": str(body)[:800],
        }) + "\n")


def set_lineup(entry, picks, chip=None, s=None, dry_run=False):
    """
    picks: 15 dicts {"element", "position" 1..15, "is_captain", "is_vice_captain"}
    Positions 1-11 are the XI, 12-15 the bench order (12 = first sub).
    """
    _validate_picks(picks)
    payload = {"chip": chip, "picks": picks}
    if dry_run:
        _audit("set_lineup[DRY]", payload, "dry", "not sent")
        return {"dry_run": True, "payload": payload}

    s = s or session()
    s.headers["Referer"] = "https://fantasy.premierleague.com/my-team"
    r = s.post(f"{BASE}/my-team/{entry}/", json=payload, timeout=30)
    _audit("set_lineup", payload, r.status_code, r.text)
    if r.status_code in (401, 403):
        raise NotAuthed(f"Token rejected on lineup write: {r.text[:200]}")
    if r.status_code >= 400:
        raise WriteRejected(f"HTTP {r.status_code}: {r.text[:400]}")
    return {"ok": True, "status": r.status_code}


def make_transfers(entry, event, moves, chip=None, s=None, dry_run=False):
    """
    moves: [{"element_in", "element_out", "purchase_price", "selling_price"}]
    Prices in FPL tenths (75 == 7.5m). selling_price must come from my-team.
    """
    if not moves:
        return {"ok": True, "noop": True}
    payload = {"confirmed": True, "entry": entry, "event": event,
               "transfers": moves, "chip": chip}
    if dry_run:
        _audit("transfers[DRY]", payload, "dry", "not sent")
        return {"dry_run": True, "payload": payload}

    s = s or session()
    s.headers["Referer"] = "https://fantasy.premierleague.com/transfers"
    r = s.post(f"{BASE}/transfers/", json=payload, timeout=30)
    _audit("transfers", payload, r.status_code, r.text)
    if r.status_code in (401, 403):
        raise NotAuthed(f"Token rejected on transfer write: {r.text[:200]}")
    if r.status_code >= 400:
        raise WriteRejected(f"HTTP {r.status_code}: {r.text[:400]}")
    return {"ok": True, "status": r.status_code}


def _validate_picks(picks):
    if len(picks) != 15:
        raise WriteRejected(f"need exactly 15 picks, got {len(picks)}")
    if sorted(p["position"] for p in picks) != list(range(1, 16)):
        raise WriteRejected("positions must be exactly 1..15 with no gaps")
    caps = [p for p in picks if p.get("is_captain")]
    vices = [p for p in picks if p.get("is_vice_captain")]
    if len(caps) != 1 or len(vices) != 1:
        raise WriteRejected("need exactly one captain and one vice-captain")
    if caps[0]["element"] == vices[0]["element"]:
        raise WriteRejected("captain and vice cannot be the same player")
    if caps[0]["position"] > 11 or vices[0]["position"] > 11:
        raise WriteRejected("captain and vice must both start")


# ---------------------------------------------------------------------- cli
def main():
    import argparse
    ap = argparse.ArgumentParser(description="FPL authenticated layer (bearer token)")
    ap.add_argument("--set-token", action="store_true", help="store a bearer token")
    ap.add_argument("--set-cookie", action="store_true",
                    help=argparse.SUPPRESS)  # legacy alias
    ap.add_argument("--check", action="store_true", help="verify the token still works")
    args = ap.parse_args()

    if args.set_token or args.set_cookie:
        if args.set_cookie:
            print("note: FPL dropped cookie auth. Reading this as --set-token.\n")
        print("HOW TO GET A TOKEN" +
              __doc__.split("HOW TO GET A TOKEN")[1].split("state/auth.json")[0])
        print("Paste below. A 'Copy as cURL' paste spans many lines --")
        print("press Enter on a blank line when done.\n")
        buf = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip() and buf:
                break
            if line.strip():
                buf.append(line)
        if not buf:
            sys.exit("nothing pasted.")
        save_token("\n".join(buf))

        try:
            entry, player = whoami()
        except NotAuthed as e:
            sys.exit(f"\n! token stored but verification failed: {e}")
        print(f"\nverified: {player.get('first_name')} {player.get('last_name')}")
        if entry:
            cfg_path = STATE / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            cfg["team_id"] = entry
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            print(f"team id {entry} saved to state/config.json")
        else:
            print("no team yet - create your squad on the FPL site, then re-run --check")
        return

    if args.check:
        try:
            entry, player = whoami()
        except NotAuthed as e:
            sys.exit(f"not authenticated: {e}")
        except requests.RequestException as e:
            sys.exit(f"network error: {e}")

        print(f"logged in as {player.get('first_name')} {player.get('last_name')}")
        print(f"token valid for another {token_hours_left():.1f}h")
        if not entry:
            print("no team created yet - nothing to read or write.")
            return
        t = my_team(entry)
        tr = t["transfers"]
        free = "unlimited" if tr["limit"] is None else tr["limit"] - tr["made"]
        print(f"entry {entry} | bank {tr['bank']/10:.1f}m | value {tr['value']/10:.1f}m | "
              f"free transfers {free}")
        avail = [c["name"] for c in t.get("chips", [])
                 if c.get("status_for_entry") == "available"]
        print("chips available:", ", ".join(avail) or "none")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
