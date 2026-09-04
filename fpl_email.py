#!/usr/bin/env python3
"""
Build (and optionally send) an HTML briefing of what the model says today.

    python fpl_email.py            # writes email.html
    python fpl_email.py --open     # and opens it in your browser
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import sys
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fpl_model as M
import fpl_auto
import urllib.parse
import fpl_activity
import fpl_inbox

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
UA = {"User-Agent": "Mozilla/5.0"}
ENTRY = 4330346

C = dict(bg="#0f1420", card="#181e2e", line="#2a3247", txt="#e8ecf5",
         dim="#8b95ad", accent="#00e0a0", warn="#ffb020", pink="#ff2882",
         good="#4ade80")


def fetch(path):
    return json.load(urllib.request.urlopen(
        urllib.request.Request("https://fantasy.premierleague.com/api/" + path,
                               headers=UA), timeout=30))


def _btn(cmd, gw, label, colour, bg, addr):
    """
    A mailto: button -- taps like a button, sends like a reply.

    Deliberately not an http link. Mail security scanners and some clients
    prefetch every URL in a message to check it is safe, so a GET that moved a
    real team would be fired by a robot reading the inbox. mailto: cannot be
    prefetched: it opens a composer and waits for a human to press send.
    """
    q = urllib.parse.urlencode({"subject": f"Re: FPL GW{gw} command", "body": cmd},
                               quote_via=urllib.parse.quote)
    return (f'<a href="mailto:{addr}?{q}" '
            f'style="display:inline-block;padding:9px 16px;margin:0 6px 6px 0;'
            f'background:{bg};color:{colour};font-size:13px;font-weight:700;'
            f'text-decoration:none;border-radius:7px;border:1px solid {colour}44;">'
            f'{label}</a>')


def _why_held(p, r, decision):
    """
    One line on why this player is still in the squad.

    "No action needed" is not an answer to that question -- it describes the
    engine, not the player. This names the actual reason each name survived:
    either nothing better was affordable, or something was and the bar stopped
    it.
    """
    if p["status"] != "a":
        return f'<span style="color:#ffb020;">flagged &middot; {html.escape((p.get("news") or "")[:38])}</span>'
    gain, who = decision.get("per_player", {}).get(p["id"], (0.0, ""))
    if gain <= 0:
        return "no affordable upgrade"
    if gain < decision["threshold"]:
        return f'best is {html.escape(who)} +{gain:.1f} &mdash; under bar'
    return (f'<span style="color:#ffb020;">{html.escape(who)} +{gain:.1f} '
            f'&middot; held at {int(decision["confidence"] * 100)}% conf</span>')


def _fdr_col(d):
    """Fixture difficulty 1-5. Green is an easy game, red is a hard one."""
    return {1: "#4ade80", 2: "#86efac", 3: "#94a3b8", 4: "#fb923c", 5: "#f87171"}.get(d, "#94a3b8")


def build():
    boot, fx, gw = M.build_fixtures(6)
    rows = M.score_all(boot, fx, 6)
    by = {r["p"]["id"]: r for r in rows}
    mine = M.owned_ids()
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    POS = M.POS

    hist = fetch(f"entry/{ENTRY}/history/")
    ev = [e for e in boot["events"] if e["id"] == gw][0]
    dl = datetime.datetime.fromisoformat(ev["deadline_time"].replace("Z", "+00:00"))
    now = datetime.datetime.now(datetime.timezone.utc)
    left = dl - now
    conf = fpl_auto.model_confidence()
    played = len([e for e in boot["events"] if e.get("finished")])

    squad = sorted((by[i] for i in mine if i in by),
                   key=lambda r: (r["p"]["element_type"], -r["ep_game"]))
    xi = {r["p"]["id"] for r in sorted(squad, key=lambda r: -r["ep_game"])[:11]}

    addr = (fpl_inbox._load(fpl_inbox.SMTP_FILE, {}).get("address")
            or "me@example.com")
    acts = fpl_activity.applied(boot)
    decision = fpl_activity.rejected(boot, rows, mine, 6)
    hb = fpl_activity.heartbeat()

    recent = [a for a in acts
              if (now - a["ts"]).days < 4 and a["type"] in ("TRANSFER", "CAPTAIN")]
    if recent:
        did = "; ".join(f'{a["type"].lower()} {a["what"]}' for a in recent[:3])
        verdict = (f'Acted since your last briefing &mdash; {html.escape(did)}. '
                   f'Nothing further is required before the deadline.')
    else:
        verdict = ('No action taken. Every player is fit, nobody is flagged, and the '
                   'lineup is unchanged. The engine only intervenes when a starter '
                   'genuinely cannot play &mdash; see below for what it considered.')

    out = []
    A = out.append

    A(f'<div style="background:{C["bg"]};padding:24px 12px;font-family:-apple-system,'
      f'BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
      f'<div style="max-width:640px;margin:0 auto;">')

    A(f'<div style="background:linear-gradient(135deg,{C["pink"]} 0%,#6a2ce0 100%);'
      f'border-radius:14px 14px 0 0;padding:22px 24px;">'
      f'<div style="color:#fff;font-size:11px;letter-spacing:2px;font-weight:700;opacity:.85;">'
      f'FPL AUTO-MANAGER</div>'
      f'<div style="color:#fff;font-size:26px;font-weight:800;margin-top:6px;">DefCon Enjoyers</div>'
      f'<div style="color:#fff;font-size:13px;opacity:.9;margin-top:4px;">'
      f'Gameweek {gw} briefing &middot; {now.strftime("%d %B %Y")}</div></div>')

    A(f'<div style="background:{C["card"]};padding:22px 24px;'
      f'border-left:1px solid {C["line"]};border-right:1px solid {C["line"]};">')

    # season strip
    cells = []
    for g in hist["current"]:
        cells.append(
            f'<td style="padding:10px 6px;text-align:center;background:{C["bg"]};border-radius:8px;">'
            f'<div style="color:{C["dim"]};font-size:10px;letter-spacing:1px;">GW{g["event"]}</div>'
            f'<div style="color:{C["accent"]};font-size:22px;font-weight:800;">{g["points"]}</div>'
            f'<div style="color:{C["dim"]};font-size:10px;">rank {g["overall_rank"]:,}</div></td>')
    total = sum(g["points"] for g in hist["current"])
    rank = hist["current"][-1]["overall_rank"] if hist["current"] else 0
    cells.append(
        f'<td style="padding:10px 6px;text-align:center;background:{C["bg"]};border-radius:8px;">'
        f'<div style="color:{C["dim"]};font-size:10px;letter-spacing:1px;">TOTAL</div>'
        f'<div style="color:{C["txt"]};font-size:22px;font-weight:800;">{total}</div>'
        f'<div style="color:{C["dim"]};font-size:10px;">OR {rank:,}</div></td>')
    A(f'<table width="100%" cellspacing="8" cellpadding="0"><tr>{"".join(cells)}</tr></table>')

    hrs = left.total_seconds() / 3600
    A(f'<div style="margin-top:18px;padding:14px 16px;background:{C["bg"]};'
      f'border-left:3px solid {C["warn"]};border-radius:6px;">'
      f'<span style="color:{C["warn"]};font-weight:700;font-size:13px;">GW{gw} DEADLINE</span>'
      f'<span style="color:{C["txt"]};font-size:13px;">&nbsp; {dl.strftime("%a %d %b, %H:%M")} UTC '
      f'&middot; {int(hrs // 24)}d {int(hrs % 24)}h away</span></div>')

    A(f'<div style="margin-top:18px;padding:16px;background:{C["bg"]};border-radius:8px;">'
      f'<div style="color:{C["accent"]};font-size:12px;font-weight:700;letter-spacing:1px;">'
      f'TODAY&rsquo;S VERDICT</div>'
      f'<div style="color:{C["txt"]};font-size:15px;margin-top:8px;line-height:1.55;">'
      f'{verdict}</div></div>')

    A(f'<div style="color:{C["txt"]};font-size:13px;font-weight:700;letter-spacing:1px;'
      f'margin:22px 0 10px;">SQUAD &middot; EXPECTED POINTS (next 6 GW)</div>'
      f'<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:13px;">'
      f'<tr style="color:{C["dim"]};font-size:10px;letter-spacing:1px;">'
      f'<th align="left" style="padding:6px 4px;">PLAYER</th><th align="left">POS</th>'
      f'<th align="right">EP/GW</th><th align="right">DEFCON</th>'
      f'<th align="left" style="padding-left:12px;">NEXT 4</th>'
      f'<th align="left" style="padding-left:12px;">WHY HELD</th></tr>')

    for r in squad:
        p = r["p"]
        start = p["id"] in xi
        dc = r["breakdown"].get("defcon", 0) / max(1, len(r["fixtures"]))
        col = C["txt"] if start else C["dim"]
        run = " ".join(
            f'<span style="color:{_fdr_col(f["difficulty"])};">'
            f'{f["opp"]}<span style="opacity:.55;">({f["ha"]})</span></span>'
            for f in r["fixtures"][:4])
        tag = "" if start else ' <span style="font-size:10px;color:#5a6478;">BENCH</span>'
        A(f'<tr style="border-top:1px solid {C["line"]};">'
          f'<td style="padding:9px 4px;color:{col};font-weight:{"600" if start else "400"};">'
          f'{html.escape(p["web_name"])}{tag}</td>'
          f'<td style="color:{C["dim"]};font-size:11px;">{POS[p["element_type"]]} &middot; '
          f'{teams[p["team"]]}</td>'
          f'<td align="right" style="color:{C["accent"] if start else C["dim"]};font-weight:700;">'
          f'{r["ep_game"]:.2f}</td>'
          f'<td align="right" style="color:{C["dim"]};font-size:11px;">{dc:.2f}</td>'
          f'<td style="padding-left:12px;color:{C["dim"]};font-size:11px;">{run}</td>'
          f'<td style="padding-left:12px;color:{C["dim"]};font-size:11px;">'
          f'{_why_held(p, r, decision)}</td></tr>')

    A(f'<tr style="border-top:2px solid {C["line"]};">'
      f'<td style="padding:10px 4px;color:{C["txt"]};font-weight:700;">SQUAD TOTAL</td><td></td>'
      f'<td align="right" style="color:{C["accent"]};font-weight:800;">'
      f'{sum(r["ep_total"] for r in squad):.0f}</td>'
      f'<td colspan="2" style="padding-left:12px;color:{C["dim"]};font-size:11px;">'
      f'over 6 gameweeks</td></tr></table>')

    pct = int(conf * 100)
    A(f'<div style="margin-top:20px;padding:14px 16px;background:{C["bg"]};'
      f'border-left:3px solid {C["dim"]};border-radius:6px;">'
      f'<div style="color:{C["dim"]};font-size:11px;font-weight:700;letter-spacing:1px;">'
      f'MODEL CONFIDENCE &middot; {pct}%</div>'
      f'<div style="width:100%;height:5px;background:{C["line"]};border-radius:3px;margin:8px 0;">'
      f'<div style="width:{max(pct, 3)}%;height:5px;background:{C["warn"]};border-radius:3px;"></div></div>'
      f'<div style="color:{C["dim"]};font-size:12px;line-height:1.5;">'
      f'Only {played} gameweek(s) played. Every per-90 rate is still shrunk heavily toward the '
      f'positional average, so rankings lean on price as much as on form. Treat these numbers as '
      f'directional until roughly GW8.</div></div>')

    # --- what it did -----------------------------------------------------
    A(f'<div style="color:{C["txt"]};font-size:13px;font-weight:700;letter-spacing:1px;'
      f'margin:26px 0 10px;">DECISIONS APPLIED &middot; LAST 21 DAYS</div>')
    if acts:
        A('<table width="100%" cellspacing="0" cellpadding="0" '
          'style="border-collapse:collapse;font-size:13px;">')
        for a in acts[:8]:
            colr = C["good"] if a["ok"] else C["warn"]
            label = a["type"] if a["ok"] else a["type"] + " REJECTED"
            A(f'<tr style="border-top:1px solid {C["line"]};">'
              f'<td style="padding:9px 4px;color:{C["dim"]};font-size:11px;width:78px;">'
              f'{a["ts"]:%d %b %H:%M}</td>'
              f'<td style="color:{colr};font-size:10px;font-weight:700;width:110px;">{label}</td>'
              f'<td style="color:{C["txt"]};">{html.escape(a["what"])}'
              + (f'<span style="color:{C["dim"]};font-size:11px;"> &middot; '
                 f'{html.escape(a["note"])}</span>' if a["note"] else "")
              + '</td></tr>')
        A('</table>')
    else:
        A(f'<div style="color:{C["dim"]};font-size:13px;">'
          f'Nothing applied in this window.</div>')

    # --- what it decided against -----------------------------------------
    A(f'<div style="color:{C["txt"]};font-size:13px;font-weight:700;letter-spacing:1px;'
      f'margin:26px 0 10px;">CONSIDERED, NOT TAKEN</div>'
      f'<div style="color:{C["dim"]};font-size:12px;line-height:1.55;margin-bottom:10px;">'
      f'{html.escape(decision["why"])}</div>')
    if decision["top"]:
        fpl_inbox.save_manifest(decision["top"], gw)
        A('<table width="100%" cellspacing="0" cellpadding="0" '
          'style="border-collapse:collapse;font-size:13px;">')
        for i, c in enumerate(decision["top"], 1):
            A(f'<tr style="border-top:1px solid {C["line"]};">'
              f'<td style="padding:8px 6px 8px 4px;color:{C["accent"]};font-weight:800;'
              f'width:22px;">{i}</td>'
              f'<td style="padding:8px 4px;color:{C["dim"]};">'
              f'{html.escape(c["out"])} &rarr; <span style="color:{C["txt"]};">'
              f'{html.escape(c["in"])}</span></td>'
              f'<td align="right" style="color:{C["accent"]};font-weight:700;">'
              f'+{c["gain"]:.1f} EP</td>'
              f'<td align="right" style="color:{C["dim"]};font-size:11px;width:64px;">'
              f'{c["cost"]:+.1f}m</td></tr>'
              f'<tr><td></td><td colspan="3" style="padding:2px 4px 10px;">'
              + _btn(f"DO {i}", gw, f"&#10003;&nbsp; DO IT", C["good"], "#16301f", addr)
              + _btn(f"NO {i}", gw, "&#10005;&nbsp; Never", C["dim"], "#20242f", addr)
              + '</td></tr>')
        A('</table>')
        A(f'<div style="margin-top:14px;padding:14px 16px;background:{C["card"]};'
          f'border:1px dashed {C["accent"]};border-radius:8px;">'
          f'<div style="color:{C["accent"]};font-size:11px;font-weight:700;'
          f'letter-spacing:1px;">OWNER OVERRIDE</div>'
          f'<div style="color:{C["txt"]};font-size:13px;line-height:1.6;margin:8px 0 12px;">'
          f'Tap a button above to approve or reject a swap. Your mail app opens '
          f'with the reply written &mdash; just press send.</div>'
          + _btn("HOLD", gw, "&#9208;&nbsp; Freeze everything", C["warn"], "#2e2312", addr)
          + _btn("RESUME", gw, "&#9654;&nbsp; Unfreeze", C["dim"], "#20242f", addr)
          + _btn("STATUS", gw, "&#8635;&nbsp; Status", C["dim"], "#20242f", addr)
          + f'<div style="color:{C["dim"]};font-size:11px;margin-top:9px;line-height:1.5;">'
            f'Or reply by hand with <code>DO 1</code> / <code>NO 1</code> / <code>HOLD</code> '
            f'on the first line &mdash; the buttons only pre-write that for you.<br>'
            f'Picked up on the next run (08:00 / 20:00 / 23:30 UTC) and re-checked against '
            f'live prices and fitness before anything is applied. Ignore this and the engine '
            f'carries on by itself &mdash; the override is yours to use, not to maintain.'
            f'</div></div>')

    # --- proof of life ---------------------------------------------------
    runs = " &middot; ".join(f'{t:%d %b %H:%M}' for t in hb["runs"][:4]) or "no runs recorded yet"
    tok = f'{hb["token_hours"]:.1f}h' if hb["token_hours"] is not None else "unknown"
    A(f'<div style="margin-top:26px;padding:14px 16px;background:{C["bg"]};'
      f'border-left:3px solid {C["good"]};border-radius:6px;">'
      f'<div style="color:{C["good"]};font-size:11px;font-weight:700;letter-spacing:1px;">'
      f'PIPELINE HEALTHY</div>'
      f'<div style="color:{C["dim"]};font-size:12px;line-height:1.6;margin-top:6px;">'
      f'Last runs (UTC): {runs}<br>'
      f'Auth token valid {tok} &middot; {hb["reports"]} daily reports on disk &middot; '
      f'{decision["free_transfers"]} free transfer(s), &pound;{decision["bank"]:.1f}m banked'
      + (f'<br><span style="color:{C["warn"]};">AUTOMATIC CHANGES ARE ON HOLD &mdash; '
         f'reply RESUME to re-enable.</span>' if fpl_inbox.is_paused() else "")
      + (f'<br>{decision["vetoed"]} proposal(s) vetoed and suppressed.'
         if decision.get("vetoed") else "")
      + f'</div></div>')

    A(f'</div><div style="background:{C["card"]};border:1px solid {C["line"]};border-top:none;'
      f'border-radius:0 0 14px 14px;padding:16px 24px;">'
      f'<div style="color:{C["dim"]};font-size:11px;line-height:1.6;">'
      f'Generated automatically &middot; runs 08:00 / 20:00 / 23:30<br>'
      f'<a href="https://fantasy.premierleague.com/entry/{ENTRY}/event/{gw}" '
      f'style="color:{C["accent"]};text-decoration:none;">View team</a> &nbsp;&middot;&nbsp; '
      f'<a href="https://github.com/kru3ish/fpl-auto-manager" '
      f'style="color:{C["accent"]};text-decoration:none;">Repo</a></div></div></div></div>')

    return "".join(out), gw, total, rank, pct


# ---------------------------------------------------------------- sending
SMTP_FILE = STATE / "smtp.json"
SENT_FILE = STATE / "last_email.json"


def setup_smtp(addr=None, pw=None):
    """Store Gmail SMTP credentials. App password, not your Google password."""
    if addr and pw:
        return _store_smtp(addr, pw)
    print("""Gmail needs an APP PASSWORD, not your account password.

  1. Google Account -> Security -> 2-Step Verification (must be on)
  2. Security -> App passwords -> create one, name it "FPL"
  3. Google shows a 16-character code. Paste it below.

This credential can ONLY send mail. It cannot read your inbox and has no
access to your FPL account. It is stored in state/smtp.json (gitignored).
""")
    addr = input("Your Gmail address: ").strip()
    pw = input("16-character app password: ").strip()
    return _store_smtp(addr, pw)


def _store_smtp(addr, pw):
    pw = pw.replace(" ", "")
    if not addr or len(pw) < 12:
        sys.exit("cancelled - address or app password looks wrong")
    STATE.mkdir(exist_ok=True)
    SMTP_FILE.write_text(json.dumps({"address": addr, "app_password": pw}), encoding="utf-8")
    try:
        SMTP_FILE.chmod(0o600)
    except OSError:
        pass
    print("saved to " + str(SMTP_FILE))
    print("testing...")
    ok, err = send(subject="FPL auto-manager: test", body_html="<p>Working.</p>",
                   body_text="Working.")
    print("  sent - check your inbox" if ok else f"  FAILED: {err}")


def action_headline():
    """First line of today's action list -- what the toast actually needs to say."""
    reports = sorted((ROOT / "reports").glob("*.md")) if (ROOT / "reports").is_dir() else []
    if not reports:
        return "Daily check complete."
    lines = reports[-1].read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## action"):
            for nxt in lines[i + 1:]:
                t = nxt.strip().lstrip("-* ").strip()
                if t and not t.startswith("#"):
                    return t[:200]
    return "Daily check complete."


def notify_desktop(title, message):
    """
    Delivery path of last resort. Email needs a credential; a notification on
    the machine that just ran the job needs nothing.

    Uses the real toast API rather than a NotifyIcon balloon tip. Balloon tips
    are transient -- they show for a few seconds, leave nothing behind, and are
    swallowed entirely by Do Not Disturb. A toast persists in Action Center
    until dismissed, which is the difference between a notification the user
    happened to be looking at and one they will actually find.
    """
    import subprocess
    aumid = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    ps = (
        "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "[void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime];"
        "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$x.LoadXml(" + _ps_quote(
            '<toast scenario="reminder"><visual><binding template="ToastGeneric">'
            "<text>" + _xml_escape(title) + "</text>"
            "<text>" + _xml_escape(message) + "</text>"
            "</binding></visual></toast>") + ");"
        "$t=New-Object Windows.UI.Notifications.ToastNotification $x;"
        "$t.Tag='fpl';$t.Group='fpl';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        + _ps_quote(aumid) + ").Show($t)"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=40, capture_output=True, text=True)
        if r.returncode == 0:
            return True
        print("  toast failed: " + (r.stderr or "").strip()[:200])
    except Exception as exc:
        print(f"  toast failed: {exc}")
    return False


def _xml_escape(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _ps_quote(v):
    return "'" + str(v).replace("'", "''") + "'"


def send(subject, body_html, body_text):
    """Send via Gmail SMTP. Returns (ok, error)."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if not SMTP_FILE.exists():
        return False, "no SMTP credentials - run: python fpl_email.py --setup-smtp"
    cfg = json.loads(SMTP_FILE.read_text(encoding="utf-8"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["address"]
    msg["To"] = cfg["address"]
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as srv:
            srv.login(cfg["address"], cfg["app_password"])
            srv.send_message(msg)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def already_sent_today():
    if not SENT_FILE.exists():
        return False
    try:
        last = json.loads(SENT_FILE.read_text(encoding="utf-8")).get("date")
    except (OSError, ValueError):
        return False
    return last == datetime.datetime.now().strftime("%Y-%m-%d")


def mark_sent():
    STATE.mkdir(exist_ok=True)
    SENT_FILE.write_text(
        json.dumps({"date": datetime.datetime.now().strftime("%Y-%m-%d")}), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--setup-smtp", action="store_true", help="store Gmail app password")
    ap.add_argument("--address", help="with --setup-smtp: Gmail address (skips the prompt)")
    ap.add_argument("--password", help="with --setup-smtp: 16-char app password (skips the prompt)")
    ap.add_argument("--send", action="store_true", help="email the briefing")
    ap.add_argument("--once-daily", action="store_true",
                    help="with --send: skip if one already went out today. The "
                         "scheduler runs 3x daily but you want one email — and if "
                         "the 08:00 run fails, 20:00 still delivers.")
    args = ap.parse_args()

    if args.setup_smtp:
        setup_smtp(args.address, args.password)
        return
    fpl_activity.beat()
    body, gw, total, rank, pct = build()
    (ROOT / "email.html").write_text(body, encoding="utf-8")
    print(f"email.html written ({len(body)} bytes)")
    print(f"GW{gw} | {total} pts | OR {rank:,} | model confidence {pct}%")
    if args.open:
        import webbrowser
        webbrowser.open((ROOT / "email.html").as_uri())

    if args.send:
        if args.once_daily and already_sent_today():
            print("already emailed today - skipping")
            return
        nl = chr(10)
        text = ("FPL AUTO-MANAGER - GW" + str(gw) + " briefing" + nl + nl
                + str(total) + " pts | overall rank " + format(rank, ",")
                + " | model confidence " + str(pct) + "%" + nl + nl
                + "Full report: " + str(ROOT / "email.html"))
        ok, err = send(f"FPL GW{gw} - {total} pts, OR {rank:,}", body, text)
        if ok:
            mark_sent()
            print("emailed")
            return

        # A missing credential is a configuration state, not a fault -- fall back
        # to the desktop and exit clean. A configured mailer that fails IS a
        # fault, and stays loud, because that is the case where the briefing is
        # silently going nowhere and nobody would know.
        print(f"email FAILED: {err}")
        headline = action_headline()
        if notify_desktop(f"FPL GW{gw} - {total} pts, OR {rank:,}", headline):
            print("notified via desktop instead")
        if not SMTP_FILE.exists():
            # Deliberately NOT marked as sent. --once-daily exists to avoid three
            # emails a day; a toast is not an email. Marking here meant the 08:00
            # run consumed the day's only attempt and the 20:00 and 23:30 runs
            # skipped silently -- so a notification missed at 8am was missed for
            # good. All three runs notify; they share a toast tag, so Action
            # Center shows one entry that updates rather than a stack.
            return
        sys.exit(1)


if __name__ == "__main__":
    main()
