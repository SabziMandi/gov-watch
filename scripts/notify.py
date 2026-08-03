#!/usr/bin/env python3
"""Email what changed in the last run.

Secrets expected in the environment (set them as GitHub repository secrets):
  SMTP_HOST   e.g. smtp.gmail.com
  SMTP_PORT   e.g. 587
  SMTP_USER   the sending address
  SMTP_PASS   an app password, never your real password
  ALERT_TO    where the mail goes

Usage: python scripts/notify.py --mode immediate
       python scripts/notify.py --mode digest
"""

import argparse
import html
import json
import os
import pathlib
import smtplib
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUN = ROOT / "data" / "run.json"


def render(events, mode):
    changes = [e for e in events if e["type"] == "change"]
    errors = [e for e in events if e["type"] == "error"]

    if mode == "immediate":
        subject = f"PIB: {len(changes)} change(s)" if changes else "gov-watch: fetch problem"
    else:
        subject = f"gov-watch digest — {len(changes)} change(s), {len(errors)} problem(s)"

    parts = ["<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
             "font-size:15px;line-height:1.5;color:#111\">"]

    for e in changes:
        parts.append(
            f"<h3 style='margin:20px 0 4px;font-size:16px'>{html.escape(e['name'])}</h3>"
            f"<p style='margin:0 0 8px;color:#555;font-size:13px'>"
            f"+{e['added']} added, -{e['removed']} removed &middot; "
            f"<a href='{html.escape(e['url'])}'>open page</a></p>"
        )
        if e.get("notes"):
            parts.append(f"<p style='margin:0 0 8px;color:#555;font-size:13px'>"
                         f"{html.escape(e['notes'])}</p>")
        for line in e.get("sample_added", [])[:6]:
            parts.append(f"<div style='background:#eaf5ea;padding:3px 8px;"
                         f"font-family:ui-monospace,monospace;font-size:13px'>+ "
                         f"{html.escape(line)}</div>")
        for line in e.get("sample_removed", [])[:6]:
            parts.append(f"<div style='background:#fbeaea;padding:3px 8px;"
                         f"font-family:ui-monospace,monospace;font-size:13px'>- "
                         f"{html.escape(line)}</div>")

    if errors:
        parts.append("<h3 style='margin:24px 0 4px;font-size:16px'>Fetch problems</h3><ul>")
        for e in errors:
            parts.append(f"<li>{html.escape(e['name'])} — {html.escape(e['detail'])}</li>")
        parts.append("</ul>")
        parts.append("<p style='color:#555;font-size:13px'>A page that fails repeatedly "
                     "is not the same as a page that has not changed. Check it by hand.</p>")

    parts.append("</div>")
    return subject, "".join(parts)


REQUIRED = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_TO"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["immediate", "digest"], default="digest")
    args = ap.parse_args()

    # A missing GitHub secret arrives as an empty string, not as an absent
    # variable. Skip quietly rather than failing the run -- losing the
    # snapshots because email is misconfigured would be the worse outcome.
    missing = [k for k in REQUIRED if not os.environ.get(k, "").strip()]
    if missing:
        print(f"email not configured, skipping ({', '.join(missing)} unset)")
        return

    events = json.loads(RUN.read_text()) if RUN.exists() else []
    if not events:
        print("nothing to send")
        return

    # Immediate alerts only fire on real changes; failures wait for the digest.
    if args.mode == "immediate" and not any(e["type"] == "change" for e in events):
        print("no changes, holding failures for the digest")
        return

    subject, body = render(events, args.mode)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["ALERT_TO"]
    msg.set_content("This message needs an HTML-capable client.")
    msg.add_alternative(body, subtype="html")

    port = int(os.environ.get("SMTP_PORT", "").strip() or 587)
    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"].strip(), port, timeout=60) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"].strip(), os.environ["SMTP_PASS"].strip())
            s.send_message(msg)
        print(f"sent: {subject}")
    except Exception as exc:  # noqa: BLE001
        # Never let a mail failure discard the run's snapshots.
        print(f"email failed, continuing: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
