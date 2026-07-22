#!/usr/bin/env python3
"""mailctl — email read/triage for Lautrec (Fastmail JMAP; Gmail later).

Unlike calctl/contactctl, mail is NOT local Apple data — it's an HTTPS API, so
there's no TCC/Full-Disk-Access problem and no compiled binary needed. A plain
script authenticated with an API token works headless under `claude -p`.

Personal mail is Fastmail via JMAP (Fastmail authored JMAP; a clean JSON API).
Token lives in config.toml [fastmail].api_token (gitignored, like the
BlueBubbles password). Read-only: unread / recent / search / one thread.
Sending is a WRITE — it requires an explicit yes from the owner.

    mailctl unread [N]                    # unread in Inbox (default 10)
    mailctl recent [N]                    # most recent N in Inbox (default 10)
    mailctl search "<query>" [N]          # full-text search
    mailctl thread <email-id>             # messages in one thread (Fastmail)
    mailctl <cmd> --account work          # work Gmail (IMAP, read-only)
    mailctl send --to a@b --subject S --body B   # Fastmail send (a WRITE)
    mailctl reply <email-id> --body B            # Fastmail in-thread reply

Personal is Fastmail via JMAP (read + send). Work Gmail is a SEPARATE source
over IMAP with a Google App Password (config.toml [gmail]), read-only — kept
distinct on purpose. Sending is Fastmail-only and is a WRITE: the persona/skill
must get an explicit yes before send/reply is ever called (never auto-send).
"""
import email
import email.utils
import imaplib
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime

try:
    import tomllib
except ModuleNotFoundError:  # <3.11 fallback
    tomllib = None

SESSION_URL = "https://api.fastmail.com/jmap/session"
CONFIG = os.environ.get(
    "LAUTREC_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "config.toml"),
)


def die(msg, code=1):
    print(json.dumps({"status": "error", "message": msg}))
    sys.exit(code)


def load_token():
    if not os.path.exists(CONFIG) or tomllib is None:
        die("config.toml not readable")
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    tok = (cfg.get("fastmail") or {}).get("api_token", "")
    if not tok or tok == "REPLACE-ME":
        die("Fastmail not provisioned. Create a JMAP API token in Fastmail "
            "(Settings › Privacy & Security › Connected apps & API tokens, "
            "'mail' read scope) and set config.toml [fastmail].api_token.")
    return tok


class JMAP:
    def __init__(self, token):
        self.token = token
        self.session = self._get(SESSION_URL)
        self.api_url = self.session["apiUrl"]
        self.account = self.session["primaryAccounts"]["urn:ietf:params:jmap:mail"]

    def _get(self, url):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            die(f"Fastmail auth/HTTP error {e.code} (token valid? scope?): {e.reason}")
        except Exception as e:
            die(f"Fastmail session fetch failed: {e}")

    def call(self, method_calls):
        body = json.dumps({
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail",
                      "urn:ietf:params:jmap:submission"],
            "methodCalls": method_calls,
        }).encode()
        req = urllib.request.Request(self.api_url, data=body, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)["methodResponses"]
        except urllib.error.HTTPError as e:
            die(f"Fastmail API error {e.code}: {e.reason}")
        except Exception as e:
            die(f"Fastmail API call failed: {e}")

    def inbox_id(self):
        return self.mailbox_by_role("inbox")

    def mailbox_by_role(self, role):
        resp = self.call([["Mailbox/query", {
            "accountId": self.account, "filter": {"role": role}}, "0"]])
        ids = resp[0][1].get("ids", [])
        return ids[0] if ids else None

    def identity(self):
        resp = self.call([["Identity/get", {"accountId": self.account}, "0"]])
        for idn in resp[0][1].get("list", []):
            if idn.get("email"):
                return idn  # {id, name, email}
        die("no send identity found (does the token have submission scope?)")

    def send(self, to, subject, body, cc=None, in_reply_to=None, references=None):
        idn = self.identity()
        drafts, sent = self.mailbox_by_role("drafts"), self.mailbox_by_role("sent")
        if not drafts or not sent:
            die("could not locate Drafts/Sent mailboxes")
        rcpt = [{"email": a} for a in to] + [{"email": a} for a in (cc or [])]
        draft = {
            "from": [{"email": idn["email"], "name": idn.get("name") or None}],
            "to": [{"email": a} for a in to],
            "subject": subject,
            "keywords": {"$draft": True, "$seen": True},
            "mailboxIds": {drafts: True},
            "bodyStructure": {"type": "text/plain", "partId": "b"},
            "bodyValues": {"b": {"value": body}},
        }
        if cc:
            draft["cc"] = [{"email": a} for a in cc]
        if in_reply_to:  # list of Message-ID strings
            draft["inReplyTo"] = in_reply_to
        if references:
            draft["references"] = references
        resp = self.call([
            ["Email/set", {"accountId": self.account, "create": {"d": draft}}, "0"],
            ["EmailSubmission/set", {"accountId": self.account,
                "onSuccessUpdateEmail": {"#s": {
                    f"mailboxIds/{drafts}": None, f"mailboxIds/{sent}": True,
                    "keywords/$draft": None}},
                "create": {"s": {"identityId": idn["id"], "emailId": "#d",
                    "envelope": {"mailFrom": {"email": idn["email"]},
                                 "rcptTo": rcpt}}}}, "1"],
        ])
        for name, r, _ in [(m[0], m[1], m[2]) for m in resp]:
            if name == "Email/set" and r.get("notCreated"):
                die(f"draft not created: {r['notCreated']}")
            if name == "EmailSubmission/set" and r.get("notCreated"):
                die(f"send failed (token needs 'submission' scope?): {r['notCreated']}")
        return idn["email"]

    def get_for_reply(self, email_id):
        resp = self.call([["Email/get", {"accountId": self.account, "ids": [email_id],
            "properties": ["messageId", "references", "subject", "from", "replyTo"]}, "0"]])
        lst = resp[0][1].get("list", [])
        if not lst:
            die("email not found")
        return lst[0]

    def query_emails(self, filt, limit):
        resp = self.call([
            ["Email/query", {"accountId": self.account, "filter": filt,
                             "sort": [{"property": "receivedAt", "isAscending": False}],
                             "limit": limit}, "0"],
            ["Email/get", {"accountId": self.account,
                           "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
                           "properties": ["id", "threadId", "from", "subject",
                                          "receivedAt", "preview", "keywords"]}, "1"],
        ])
        for r in resp:
            if r[0] == "Email/get":
                return r[1].get("list", [])
        return []


def to_local(iso_utc):
    """JMAP receivedAt is UTC (…Z) → local naive ISO, matching calctl."""
    if not iso_utc:
        return ""
    try:
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return iso_utc


def fmt(emails):
    out = []
    for e in emails:
        frm = (e.get("from") or [{}])[0]
        out.append({
            "id": e.get("id"),
            "threadId": e.get("threadId"),
            "from": frm.get("name") or frm.get("email") or "",
            "fromEmail": frm.get("email") or "",
            "subject": e.get("subject") or "(no subject)",
            "received": to_local(e.get("receivedAt") or ""),
            "unread": not (e.get("keywords") or {}).get("$seen", False),
            "preview": (e.get("preview") or "").strip()[:200],
        })
    return out


def plain(emails):
    if not emails:
        print("Nothing.")
        return
    for e in emails:
        mark = "•" if e["unread"] else " "
        when = e["received"][:16].replace("T", " ")
        print(f"{mark} {when}  {e['from']}: {e['subject']}")


# ---------------------------------------------------------------- Gmail (work)

def load_gmail():
    if not os.path.exists(CONFIG) or tomllib is None:
        die("config.toml not readable")
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    g = cfg.get("gmail") or {}
    addr, pw = g.get("address", ""), g.get("app_password", "")
    if not addr or not pw or pw == "REPLACE-ME":
        die("Work Gmail not provisioned. If your Workspace allows App Passwords "
            "(myaccount.google.com › Security › App passwords, needs 2FA), create "
            "one and set config.toml [gmail].address + [gmail].app_password. If "
            "App Passwords are blocked by admin, say so — Gmail needs the "
            "OAuth path instead.")
    return addr, pw


def gmail_fetch(cmd, query, n):
    addr, pw = load_gmail()
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(addr, pw)
    except imaplib.IMAP4.error as e:
        die(f"Gmail IMAP login failed (App Password valid? IMAP enabled? "
            f"Workspace may block it — then use OAuth): {e}")
    try:
        M.select("INBOX", readonly=True)
        if cmd == "unread":
            _, data = M.search(None, "UNSEEN")
        elif cmd == "recent":
            _, data = M.search(None, "ALL")
        elif cmd == "search":
            _, data = M.search(None, "TEXT", query)
        else:
            die(f"'{cmd}' not supported for work Gmail (use unread/recent/search)")
        ids = data[0].split()
        ids = ids[-n:][::-1]  # newest first
        out = []
        for i in ids:
            _, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")
            raw = next((p[1] for p in md if isinstance(p, tuple)), b"")
            flags = imaplib.ParseFlags(next((p[0] for p in md if isinstance(p, tuple)), b"") or b"")
            msg = email.message_from_bytes(raw)
            name, mail_addr = email.utils.parseaddr(msg.get("From", ""))
            recv = ""
            try:
                dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                recv = dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S") if dt else ""
            except (TypeError, ValueError):
                pass
            out.append({
                "id": i.decode(), "threadId": None,
                "from": name or mail_addr, "fromEmail": mail_addr,
                "subject": msg.get("Subject", "(no subject)"),
                "received": recv,
                "unread": b"\\Seen" not in flags,
                "preview": "",
            })
        return out
    finally:
        try: M.logout()
        except Exception: pass


def main():
    args = sys.argv[1:]
    as_plain = "--plain" in args
    args = [a for a in args if a != "--plain"]
    account = "personal"
    if "--account" in args:
        i = args.index("--account"); account = args[i + 1].lower(); del args[i:i + 2]
    if "--format" in args:
        i = args.index("--format"); as_plain = args[i + 1].lower() != "json"; del args[i:i + 2]
    cmd = args[0] if args else "unread"

    # Work Gmail (IMAP) — separate source.
    if account in ("work", "gmail"):
        if cmd == "unread":
            n = int(args[1]) if len(args) > 1 else 10
            emails = gmail_fetch("unread", None, n)
        elif cmd == "recent":
            n = int(args[1]) if len(args) > 1 else 10
            emails = gmail_fetch("recent", None, n)
        elif cmd == "search":
            if len(args) < 2:
                die('usage: mailctl search "<query>" --account work')
            n = int(args[2]) if len(args) > 2 else 15
            emails = gmail_fetch("search", args[1], n)
        else:
            die(f"'{cmd}' not supported for work Gmail (unread/recent/search only)")
        result = emails
        if as_plain:
            plain(result)
        else:
            print(json.dumps({"status": "ok", "account": "work", "count": len(result),
                              "emails": result}, indent=2, ensure_ascii=False))
        return

    jmap = JMAP(load_token())

    # --- Fastmail send / reply (a WRITE — persona must confirm first) ---
    if cmd in ("send", "reply"):
        def flag(name):
            if name in args:
                i = args.index(name)
                if i + 1 < len(args):
                    return args[i + 1]
            return None
        body = flag("--body")
        if body is None:
            die('--body is required (e.g. mailctl send --to a@b.com --subject "Hi" --body "…")')
        if cmd == "send":
            to, subject, cc = flag("--to"), flag("--subject"), flag("--cc")
            if not to or subject is None:
                die('usage: mailctl send --to <a,b> --subject "…" --body "…" [--cc <a,b>]')
            frm = jmap.send([t.strip() for t in to.split(",") if t.strip()], subject, body,
                            cc=[c.strip() for c in cc.split(",")] if cc else None)
            res = {"status": "ok", "sent": True, "from": frm,
                   "to": [t.strip() for t in to.split(",")], "subject": subject}
        else:  # reply
            if len(args) < 2 or args[1].startswith("--"):
                die('usage: mailctl reply <email-id> --body "…"')
            orig = jmap.get_for_reply(args[1])
            addrs = orig.get("replyTo") or orig.get("from") or []
            to = [a["email"] for a in addrs if a.get("email")]
            if not to:
                die("original message has no reply address")
            subj = orig.get("subject") or ""
            if not subj.lower().startswith("re:"):
                subj = "Re: " + subj
            refs = (orig.get("references") or []) + (orig.get("messageId") or [])
            frm = jmap.send(to, subj, body, in_reply_to=orig.get("messageId"),
                            references=refs or None)
            res = {"status": "ok", "sent": True, "from": frm, "to": to, "subject": subj}
        if as_plain:
            print(f"Sent to {', '.join(res['to'])}: {res['subject']}")
        else:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    inbox = jmap.inbox_id()
    if not inbox and cmd in ("unread", "recent"):
        die("no Inbox mailbox found")

    if cmd == "unread":
        n = int(args[1]) if len(args) > 1 else 10
        emails = jmap.query_emails({"inMailbox": inbox, "notKeyword": "$seen"}, n)
    elif cmd == "recent":
        n = int(args[1]) if len(args) > 1 else 10
        emails = jmap.query_emails({"inMailbox": inbox}, n)
    elif cmd == "search":
        if len(args) < 2:
            die('usage: mailctl search "<query>" [N]')
        n = int(args[2]) if len(args) > 2 else 15
        emails = jmap.query_emails({"text": args[1]}, n)
    elif cmd == "thread":
        if len(args) < 2:
            die("usage: mailctl thread <email-id>")
        got = jmap.call([["Email/get", {"accountId": jmap.account, "ids": [args[1]],
                        "properties": ["threadId"]}, "0"]])
        tid = (got[0][1].get("list") or [{}])[0].get("threadId")
        if not tid:
            die("email not found")
        emails = jmap.query_emails({"inThread": tid}, 50)
    else:
        die(f"unknown command: {cmd}. Commands: unread, recent, search, thread")

    result = fmt(emails)
    if as_plain:
        plain(result)
    else:
        print(json.dumps({"status": "ok", "count": len(result), "emails": result},
                         indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
