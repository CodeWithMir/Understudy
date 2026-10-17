"""Shows the mails Understudy wrote to its local outbox (UNDERSTUDY_MAIL_MODE=outbox - development only).

    python scripts/mail_outbox.py            # last 10 mails
    python scripts/mail_outbox.py -n 3       # last 3
    python scripts/mail_outbox.py --to alice@example.com

Run it on the server that runs Understudy. Codes are only in this file, never in the API or Mission Control."""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.wizard import mailer  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("-n", type=int, default=10)
ap.add_argument("--to", default="")
a = ap.parse_args()
rows = [r for r in mailer.outbox(500) if not a.to or r["to"] == a.to.lower()][-a.n:]
print(f"outbox: {mailer.config()['outbox']} ({mailer.config()['mode']} mode)")
if not rows:
    print("(empty)")
for r in rows:
    print("-" * 72)
    print(datetime.datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d %H:%M:%S"), "|", r["to"], "|", r["subject"])
    print(r["text"].strip())
