#!/usr/bin/env python3
"""
Automatic bearer-token rotation via OIDC refresh tokens.

FPL access tokens live 8 hours. But FPL's login is standard OIDC (Ping Identity,
via oidc-client-ts), and the discovery document advertises the refresh_token
grant. So we don't need a browser to rotate at all -- one POST does it:

    POST https://account.premierleague.com/as/token
    grant_type=refresh_token & client_id=... & refresh_token=...

Verified reachable from a plain script: Cloudflare passes it through.

SETUP -- once, no password, no browser automation:
  1. Log in at https://fantasy.premierleague.com
  2. F12 -> Console tab
  3. Paste this and hit Enter (it copies the refresh token to your clipboard):

     copy(Object.entries(localStorage).filter(([k])=>k.startsWith('oidc.user'))
       .map(([,v])=>JSON.parse(v).refresh_token)[0])

  4. python fpl_token.py --set-refresh      then paste

After that:
    python fpl_token.py --refresh   # mints a fresh access token, no browser
    python fpl_token.py --status

Refresh tokens ROTATE: each exchange returns a new one, and the old one dies.
This script always persists the new one immediately. If the chain is ever broken
(you log out everywhere, change your password, or the token sits unused past its
own expiry), --refresh fails loudly and you redo the 30-second setup above.

state/auth.json holds the refresh token. It is a long-lived credential -- anyone
with it can mint access tokens for your account until you log out. Treat it as a
password.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

import fpl_write

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
AUTH_FILE = STATE / "auth.json"
PROFILE = STATE / "browser"

ISSUER = "https://account.premierleague.com/as"
TOKEN_URL = f"{ISSUER}/token"
CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
REFRESH_BELOW_HOURS = 2.0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://fantasy.premierleague.com",
    "Referer": "https://fantasy.premierleague.com/",
}

# Scans BOTH storages for any object carrying a refresh_token, and reports what
# it found rather than silently copying "undefined".
CONSOLE_SNIPPET = (
    "(()=>{const o=[];for(const [w,s] of [['local',localStorage],"
    "['session',sessionStorage]]){for(const k of Object.keys(s)){let v;"
    "try{v=JSON.parse(s[k])}catch(e){continue}"
    "if(v&&v.refresh_token)o.push({where:w,key:k,rt:v.refresh_token});}}"
    "if(!o.length){console.log('NO refresh_token found.',"
    "'local:',Object.keys(localStorage),'session:',Object.keys(sessionStorage));return;}"
    "copy(o[0].rt);console.log('COPIED from '+o[0].where+'Storage:',"
    "o[0].rt.slice(0,20)+'...('+o[0].rt.length+' chars)');})()"
)


# ------------------------------------------------------------------ auth store
def _read():
    if AUTH_FILE.exists():
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    return {}


def _write(d):
    STATE.mkdir(exist_ok=True)
    AUTH_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    try:
        AUTH_FILE.chmod(0o600)
    except OSError:
        pass


def save_refresh(tok: str):
    tok = tok.strip().strip('"').strip("'")
    if not tok or len(tok) < 20 or " " in tok:
        sys.exit("That doesn't look like a refresh token.\n"
                 "Run the console snippet in --help and paste what it copies.")
    d = _read()
    # Ping issues JWT refresh tokens too, so shape alone can't tell them apart.
    # The only unambiguous mistake is pasting back the access token we already
    # hold; anything else gets validated for real by the exchange below.
    if tok == d.get("token"):
        sys.exit("That's the ACCESS token you already have, not the refresh token.\n"
                 "Access tokens die in 8h and can't rotate. Re-run the console\n"
                 "snippet -- it reads the refresh_token field specifically.")

    # Refresh tokens rotate: once we exchange one it is dead server-side, and
    # the browser's localStorage keeps showing the dead copy until the page
    # re-authenticates. Catch that here rather than after a failed exchange.
    jti = fpl_write.decode_jwt(tok).get("jti")
    if jti and jti in d.get("consumed_jti", []):
        sys.exit(
            "This is the SAME refresh token you already used -- it was consumed\n"
            "and rotated, so it no longer exists server-side.\n\n"
            "Your browser is still showing the dead copy. Force it to mint a new one:\n"
            "  1. Go to https://fantasy.premierleague.com\n"
            "  2. Hard reload: Ctrl+Shift+R\n"
            "  3. If it logs you out, log back in\n"
            "  4. Re-run the console snippet -- the char count should differ\n"
            "  5. python fpl_token.py --set-refresh")

    d["refresh_token"] = tok
    _write(d)
    print(f"refresh token saved to {AUTH_FILE}")


# ------------------------------------------------------------------- exchange
def exchange(refresh_token: str):
    """Swap a refresh token for a new access token. Returns (access, new_refresh)."""
    r = requests.post(TOKEN_URL, headers=HEADERS, timeout=30, data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    })
    if r.status_code >= 400:
        try:
            err = r.json()
            detail = f"{err.get('error')}: {err.get('error_description')}"
        except Exception:
            detail = r.text[:200]
        raise RuntimeError(f"HTTP {r.status_code} — {detail}")
    body = r.json()
    access = body.get("access_token")
    if not access:
        raise RuntimeError(f"no access_token in response: {list(body)}")
    # Ping rotates refresh tokens; keep the new one or the chain breaks.
    return access, body.get("refresh_token") or refresh_token


def _profile_relogin():
    """
    Silently re-mint from the dedicated browser profile.

    The whole fragility is that a refresh token obtained from YOUR everyday
    browser shares one rotation chain with that browser. Ping rotates on every
    exchange, so the next time you open the FPL site the SPA mints a new token
    and ours dies. state/browser/ is a SEPARATE profile with its own Ping
    session, so its chain is independent of your browsing -- and because the
    session cookie long outlives the 8h token, we can re-mint headlessly with
    no interaction at all.
    """
    if not PROFILE.exists():
        return None
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return None
    import time

    found = {}
    try:
        with sync_playwright() as pw:
            launch = dict(user_data_dir=str(PROFILE), headless=True,
                          viewport={"width": 1360, "height": 900},
                          args=["--disable-blink-features=AutomationControlled"])
            try:
                ctx = pw.chromium.launch_persistent_context(channel="chrome", **launch)
            except Exception:
                ctx = pw.chromium.launch_persistent_context(**launch)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def on_request(req):
                a = req.headers.get("x-api-authorization", "")
                if "tok" not in found and a.lower().startswith("bearer "):
                    found["tok"] = a.split(None, 1)[1]

            page.on("request", on_request)
            try:
                page.goto("https://fantasy.premierleague.com/my-team",
                          wait_until="domcontentloaded", timeout=45000)
            except PWTimeout:
                pass
            deadline = time.time() + 45
            while time.time() < deadline and "tok" not in found:
                page.wait_for_timeout(500)
            try:
                rt = page.evaluate(
                    "Object.entries(localStorage).filter(([k])=>k.startsWith('oidc.user'))"
                    ".map(([,v])=>JSON.parse(v).refresh_token)[0] || null")
                if rt:
                    d = _read(); d["refresh_token"] = rt; _write(d)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    except Exception:
        return None
    return found.get("tok")


def do_refresh(force=False):
    d = _read()
    rt = d.get("refresh_token")
    if not rt:
        print("No refresh token stored. One-time setup:")
        print(f"\n  1. Log in at https://fantasy.premierleague.com")
        print(f"  2. F12 -> Console, paste:\n\n     {CONSOLE_SNIPPET}\n")
        print(f"  3. python fpl_token.py --set-refresh")
        return False

    left = fpl_write.token_hours_left()
    if not force and left >= REFRESH_BELOW_HOURS:
        print(f"access token still valid for {left:.1f}h - no refresh needed")
        return True

    old_jti = fpl_write.decode_jwt(rt).get("jti")
    try:
        access, new_rt = exchange(rt)
    except Exception as e:
        # remember it so a re-paste of the same dead token is caught up front
        if old_jti:
            d.setdefault("consumed_jti", [])
            if old_jti not in d["consumed_jti"]:
                d["consumed_jti"] = (d["consumed_jti"] + [old_jti])[-20:]
                _write(d)
        # self-heal from the isolated profile before troubling the human
        healed = _profile_relogin()
        if healed:
            fpl_write.save_token(healed)
            print("chain was broken; re-minted from the isolated browser profile")
            _report()
            return True

        print(f"REFRESH FAILED - {e}")
        if "does not exist" in str(e) or "invalid_grant" in str(e):
            print("\nThat refresh token was already used. Rotation kills the old one,")
            print("and your BROWSER still has the dead copy cached - so re-copying")
            print("it gives you the same dead token again.")
            print("\nForce the browser to mint a new one:")
            print("  1. https://fantasy.premierleague.com")
            print("  2. Hard reload: Ctrl+Shift+R  (log back in if it signs you out)")
            print("  3. Re-run the snippet - the char count should be DIFFERENT")
            print("  4. python fpl_token.py --set-refresh")
        else:
            print("\nRedo the setup:")
            print(f"  F12 -> Console:  {CONSOLE_SNIPPET}")
            print("  python fpl_token.py --set-refresh")
        return False

    if old_jti:
        d.setdefault("consumed_jti", [])
        d["consumed_jti"] = (d["consumed_jti"] + [old_jti])[-20:]
    d["refresh_token"] = new_rt
    _write(d)
    fpl_write.save_token(access)
    if new_rt != rt:
        print("(refresh token rotated and saved)")
    _report()
    return True


def _report():
    try:
        entry, player = fpl_write.whoami()
    except Exception as e:
        print(f"! token saved but verification failed: {e}")
        return
    print(f"verified: {player.get('first_name')} {player.get('last_name')}"
          + (f" | entry {entry}" if entry else " | no team created yet"))


# --------------------------------------------------- browser fallback (rare)
def do_login():
    """Last resort if the refresh chain dies and you'd rather not use DevTools."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        sys.exit("Browser fallback needs: pip install playwright && "
                 "python -m playwright install chromium\n"
                 "The --set-refresh route needs no extra packages.")
    import time

    STATE.mkdir(exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    found = {}
    print("A browser opens. Log in; this window closes itself once it sees a token.")

    with sync_playwright() as pw:
        launch = dict(user_data_dir=str(PROFILE), headless=False,
                      viewport={"width": 1360, "height": 900},
                      args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = pw.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception:
            ctx = pw.chromium.launch_persistent_context(**launch)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            auth = req.headers.get("x-api-authorization", "")
            if "tok" not in found and auth.lower().startswith("bearer "):
                found["tok"] = auth.split(None, 1)[1]

        page.on("request", on_request)
        try:
            page.goto("https://fantasy.premierleague.com/my-team",
                      wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            pass

        deadline = time.time() + 300
        while time.time() < deadline and "tok" not in found:
            page.wait_for_timeout(500)

        # grab the refresh token too, so future rotations need no browser
        try:
            rt = page.evaluate(
                "Object.entries(localStorage).filter(([k])=>k.startsWith('oidc.user'))"
                ".map(([,v])=>JSON.parse(v).refresh_token)[0] || null")
            if rt:
                d = _read()
                d["refresh_token"] = rt
                _write(d)
                print("refresh token captured - future rotations need no browser")
        except Exception:
            pass

        try:
            ctx.close()
        except Exception:
            pass

    if not found:
        sys.exit("No token seen. Did the login complete?")
    fpl_write.save_token(found["tok"])
    _report()


def main():
    ap = argparse.ArgumentParser(
        description="FPL token rotation (OIDC refresh tokens)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"one-time setup:\n  F12 -> Console:  {CONSOLE_SNIPPET}\n"
               f"  python fpl_token.py --set-refresh")
    ap.add_argument("--set-refresh", action="store_true", help="store your refresh token")
    ap.add_argument("--refresh", action="store_true", help="mint a fresh access token")
    ap.add_argument("--force", action="store_true", help="refresh even if not near expiry")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--login", action="store_true",
                    help="browser fallback if the refresh chain dies")
    args = ap.parse_args()

    if args.set_refresh:
        print("Log in at fantasy.premierleague.com, then in the DevTools Console run:\n")
        print(f"  {CONSOLE_SNIPPET}\n")
        print("It copies the refresh token to your clipboard. Paste it here:")
        raw = input("> ")
        save_refresh(raw)
        do_refresh(force=True)
    elif args.refresh:
        sys.exit(0 if do_refresh(force=args.force) else 1)
    elif args.login:
        do_login()
    elif args.status:
        d = _read()
        left = fpl_write.token_hours_left()
        print(f"access token : {'valid ' + format(left, '.1f') + 'h' if left else 'none/expired'}")
        print(f"refresh token: {'stored' if d.get('refresh_token') else 'NOT SET'}")
        if not d.get("refresh_token"):
            print(f"\nset it up:  F12 -> Console:  {CONSOLE_SNIPPET}")
            print(f"            python fpl_token.py --set-refresh")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
