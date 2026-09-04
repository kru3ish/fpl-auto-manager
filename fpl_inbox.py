#!/usr/bin/env python3
"""
The owner's veto. Reply to the briefing; the next run obeys.

The engine proposes and, below a confidence bar, declines to act. That leaves
decisions sitting in an email with no way to say yes. This reads replies to the
briefing over IMAP and executes what the owner approved -- so the last word is
available without being required. Ignore the email and the engine still runs.

    DO 2          execute proposal 2 from the latest briefing
    NO 2          veto proposal 2 for 14 days; stops it being re-proposed daily
    HOLD          suspend all automatic changes
    RESUME        re-enable them
    STATUS        reply with the current state

WHY IMAP AND NOT A LINK. A clickable link needs a server that is reachable from
a phone, which means hosting something with a public endpoint that can move a
real team. A reply is authenticated by the mail account that already exists and
reaches nothing but this machine.

TRUST BOUNDARY. Commands are honoured only when the sender is exactly the
configured address, and only against proposals this system itself wrote to the
manifest -- never against instructions parsed out of message text. A reply
cannot invent a transfer; it can only pick a numbered option the engine already
put on the table and still considers legal at execution time. Anyone able to
forge that From: header already holds the mailbox the briefings are sent to.
"""

from __future__ import annotations

import datetime
import email
import email.utils
import imaplib
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
PENDING = STATE / "pending_decisions.json"
VETOES = STATE / "vetoes.json"
PAUSE = STATE / "paused.json"
SEEN = STATE / "seen_commands.json"
SMTP_FILE = STATE / "smtp.json"

IMAP_HOST = "imap.gmail.com"
VETO_DAYS = 14

CMD = re.compile(r"^\s*>*\s*(DO|NO|HOLD|RESUME|STATUS)\b\s*(\d+)?", re.I | re.M)


def _load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _save(path, obj):
    STATE.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def save_manifest(decisions, gw):
    """
    Freeze the numbered proposals at send time.

    The numbers in the email have to mean the same thing when the reply arrives
    hours later, and the model re-ranks on every run. Executing against a
    freshly computed list would let 'DO 2' apply a different transfer than the
    one the owner read.
    """
    _save(PENDING, {
        "gw": gw,
        "issued": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "decisions": decisions,
    })


def vetoed():
    """Proposal keys the owner has rejected and that have not yet expired."""
    now = datetime.datetime.now(datetime.timezone.utc)
    live = {}
    for key, until in _load(VETOES, {}).items():
        try:
            if datetime.datetime.fromisoformat(until) > now:
                live[key] = until
        except ValueError:
            continue
    return live


def is_paused():
    return bool(_load(PAUSE, {}).get("paused"))


def _connect():
    cfg = _load(SMTP_FILE, {})
    if not cfg.get("address") or not cfg.get("app_password"):
        raise RuntimeError("no mail credentials stored")
    box = imaplib.IMAP4_SSL(IMAP_HOST)
    box.login(cfg["address"], cfg["app_password"])
    return box, cfg["address"]


def _body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def _first_command(text):
    """
    Take only the first command in the message.

    Replies quote the briefing beneath them, and the briefing lists the command
    words. Scanning the whole body would read the owner's own instructions back
    out of the quoted text and execute them.
    """
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            break
        m = CMD.match(line)
        if m:
            return m.group(1).upper(), (int(m.group(2)) if m.group(2) else None)
    return None, None


def _manifest_issued():
    try:
        return datetime.datetime.fromisoformat(_load(PENDING, {})["issued"])
    except (KeyError, ValueError):
        return None


def _sent_at(msg):
    try:
        return email.utils.parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        return None


def poll(apply=False):
    """Read unread replies and return the commands found, newest last."""
    box, addr = _connect()
    found = []
    try:
        box.select("INBOX")
        # Gmail's IMAP SUBJECT search tokenises rather than matching substrings,
        # so 'FPL GW' finds nothing against 'Re: FPL GW3 command' -- GW and GW3
        # are different tokens. Search by sender and date, filter the subject
        # here. UNSEEN is not usable either: mail you send yourself arrives
        # already flagged \Seen, so every button press looked read. The
        # Message-ID ledger, not the read flag, is what stops re-execution.
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=3)).strftime("%d-%b-%Y")
        typ, data = box.search(None, f'(FROM "{addr}" SINCE {since})')
        if typ != "OK":
            return found
        seen = set(_load(SEEN, []))
        issued = _manifest_issued()
        for num in data[0].split():
            typ, raw = box.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(raw[0][1])
            sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            mid = msg.get("Message-ID", "")
            subj = msg.get("Subject", "")

            # Only the owner's own address, only once per message, only replies
            # to a briefing.
            if sender != addr.lower() or mid in seen or "FPL" not in subj.upper():
                continue

            # And only commands written after the proposals they refer to. The
            # numbers are re-issued every briefing, so an old "DO 1" left in the
            # mailbox would otherwise execute against whatever is first on
            # today's list -- the exact mismatch the manifest exists to prevent.
            when = _sent_at(msg)
            if issued and when and when < issued:
                continue
            verb, idx = _first_command(_body(msg))
            if not verb:
                continue
            found.append({"verb": verb, "idx": idx, "mid": mid,
                          "subject": msg.get("Subject", "")})
            if apply:
                seen.add(mid)
                box.store(num, "+FLAGS", "\\Seen")
        if apply:
            _save(SEEN, sorted(seen)[-200:])
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return found


def execute(cmd, dry_run=True):
    """Carry out one command. Returns a human-readable result line."""
    verb, idx = cmd["verb"], cmd["idx"]

    if verb == "HOLD":
        if not dry_run:
            _save(PAUSE, {"paused": True,
                          "since": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        return "Automatic changes suspended. Reply RESUME to re-enable."
    if verb == "RESUME":
        if not dry_run:
            _save(PAUSE, {"paused": False})
        return "Automatic changes re-enabled."
    if verb == "STATUS":
        man = _load(PENDING, {})
        return (f'GW{man.get("gw", "?")} · {len(man.get("decisions", []))} proposals open · '
                f'{"PAUSED" if is_paused() else "running"} · '
                f'{len(vetoed())} active veto(es).')

    man = _load(PENDING, {})
    decisions = man.get("decisions", [])
    if idx is None or not (1 <= idx <= len(decisions)):
        return f"No proposal {idx} in the last briefing ({len(decisions)} were listed)."
    d = decisions[idx - 1]

    if verb == "NO":
        if not dry_run:
            v = _load(VETOES, {})
            until = (datetime.datetime.now(datetime.timezone.utc)
                     + datetime.timedelta(days=VETO_DAYS))
            v[d["key"]] = until.isoformat()
            _save(VETOES, v)
        return f'Vetoed {d["label"]} for {VETO_DAYS} days. It will not be re-proposed.'

    # DO -- re-validate against live state. The briefing may be hours old; a
    # player can be flagged, priced out, or the deadline can have passed.
    import fpl_write as W
    import fpl_model as M

    entry = _load(STATE / "config.json", {}).get("team_id")
    boot, _fx, gw = M.build_fixtures(1)
    el = {e["id"]: e for e in boot["elements"]}
    pin, pout = el.get(d["in_id"]), el.get(d["out_id"])
    if not pin or not pout:
        return f'Cannot apply {d["label"]}: player no longer in the game.'
    if pin["status"] != "a":
        return (f'Refused {d["label"]}: {pin["web_name"]} is now flagged '
                f'({pin.get("news") or pin["status"]}).')

    team = W.my_team(entry)
    sell = {p["element"]: p["selling_price"] for p in team["picks"]}
    bank = team["transfers"]["bank"]
    if d["out_id"] not in sell:
        return f'Cannot apply {d["label"]}: {pout["web_name"]} is no longer in the squad.'
    if pin["now_cost"] > bank + sell[d["out_id"]]:
        return (f'Refused {d["label"]}: {pin["web_name"]} now costs '
                f'£{pin["now_cost"] / 10:.1f}m, over budget.')

    moves = [{"element_in": d["in_id"], "element_out": d["out_id"],
              "purchase_price": pin["now_cost"], "selling_price": sell[d["out_id"]]}]
    if dry_run:
        return f'[DRY RUN] would apply {d["label"]}.'
    # make_transfers raises on rejection rather than returning a status, so a
    # refusal has to be caught here or the whole poll dies on one bad command.
    try:
        W.make_transfers(entry, gw, moves, dry_run=False)
    except Exception as exc:
        return f'FPL rejected {d["label"]}: {str(exc)[:180]}'
    return f'Applied {d["label"]}.' 


def main():
    import argparse
    ap = argparse.ArgumentParser(description="read owner commands from email")
    ap.add_argument("--apply", action="store_true", help="actually execute")
    args = ap.parse_args()

    try:
        cmds = poll(apply=args.apply)
    except Exception as exc:
        print(f"inbox unavailable: {exc}")
        return

    if not cmds:
        print("no owner commands")
        return

    results = []
    for c in cmds:
        line = execute(c, dry_run=not args.apply)
        print(f'{c["verb"]}{" " + str(c["idx"]) if c["idx"] else ""} -> {line}')
        results.append(line)

    if args.apply and results:
        try:
            import fpl_email as E
            E.send(subject="FPL: command received",
                   body_html="<p>" + "<br>".join(results) + "</p>",
                   body_text="\n".join(results))
        except Exception as exc:
            print(f"could not confirm by email: {exc}")


if __name__ == "__main__":
    main()
