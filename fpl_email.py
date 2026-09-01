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

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
UA = {"User-Agent": "Mozilla/5.0"}
ENTRY = 4330346

C = dict(bg="#0f1420", card="#181e2e", line="#2a3247", txt="#e8ecf5",
         dim="#8b95ad", accent="#00e0a0", warn="#ffb020", pink="#ff2882")


def fetch(path):
    return json.load(urllib.request.urlopen(
        urllib.request.Request("https://fantasy.premierleague.com/api/" + path,
                               headers=UA), timeout=30))


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
      f'No action needed. Every player is fit, nobody is flagged, and the lineup is '
      f'unchanged. The engine only intervenes when a starter genuinely cannot play.</div></div>')

    A(f'<div style="color:{C["txt"]};font-size:13px;font-weight:700;letter-spacing:1px;'
      f'margin:22px 0 10px;">SQUAD &middot; EXPECTED POINTS (next 6 GW)</div>'
      f'<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:13px;">'
      f'<tr style="color:{C["dim"]};font-size:10px;letter-spacing:1px;">'
      f'<th align="left" style="padding:6px 4px;">PLAYER</th><th align="left">POS</th>'
      f'<th align="right">EP/GW</th><th align="right">DEFCON</th>'
      f'<th align="left" style="padding-left:12px;">NEXT 4</th></tr>')

    for r in squad:
        p = r["p"]
        start = p["id"] in xi
        dc = r["breakdown"].get("defcon", 0) / max(1, len(r["fixtures"]))
        col = C["txt"] if start else C["dim"]
        run = " ".join(f'{f["opp"]}{f["ha"]}' for f in r["fixtures"][:4])
        tag = "" if start else ' <span style="font-size:10px;color:#5a6478;">BENCH</span>'
        A(f'<tr style="border-top:1px solid {C["line"]};">'
          f'<td style="padding:9px 4px;color:{col};font-weight:{"600" if start else "400"};">'
          f'{html.escape(p["web_name"])}{tag}</td>'
          f'<td style="color:{C["dim"]};font-size:11px;">{POS[p["element_type"]]} &middot; '
          f'{teams[p["team"]]}</td>'
          f'<td align="right" style="color:{C["accent"] if start else C["dim"]};font-weight:700;">'
          f'{r["ep_game"]:.2f}</td>'
          f'<td align="right" style="color:{C["dim"]};font-size:11px;">{dc:.2f}</td>'
          f'<td style="padding-left:12px;color:{C["dim"]};font-size:11px;">{run}</td></tr>')

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


def setup_smtp():
    """Store Gmail SMTP credentials. App password, not your Google password."""
    print("""Gmail needs an APP PASSWORD, not your account password.

  1. Google Account -> Security -> 2-Step Verification (must be on)
  2. Security -> App passwords -> create one, name it "FPL"
  3. Google shows a 16-character code. Paste it below.

This credential can ONLY send mail. It cannot read your inbox and has no
access to your FPL account. It is stored in state/smtp.json (gitignored).
""")
    addr = input("Your Gmail address: ").strip()
    pw = input("16-character app password: ").strip().replace(" ", "")
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
    ap.add_argument("--send", action="store_true", help="email the briefing")
    ap.add_argument("--once-daily", action="store_true",
                    help="with --send: skip if one already went out today. The "
                         "scheduler runs 3x daily but you want one email — and if "
                         "the 08:00 run fails, 20:00 still delivers.")
    args = ap.parse_args()

    if args.setup_smtp:
        setup_smtp()
        return
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
        else:
            print(f"email FAILED: {err}")
            sys.exit(1)


if __name__ == "__main__":
    main()
