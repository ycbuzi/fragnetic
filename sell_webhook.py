"""Fragnetic SALE -> LICENSE KEY webhook receiver.

Runs on YOUR OWN small server (NOT shipped to customers, NOT part of the app).
Receives a purchase webhook from your payment processor (default: Lemon
Squeezy), verifies its signature, mints a real Ed25519 license key with the
SAME engine `mint_license.py` uses, and emails it to the buyer.

>>> FILL IN THE 4 TODOs BELOW BEFORE RUNNING THIS FOR REAL. <<<

Run:  py -3 sell_webhook.py            (listens on :8899 by default)
Test: use your processor's sandbox/test-webhook feature to fire a sample event
      at http://your-server:8899/webhook, then check your test inbox.

Pure stdlib (http.server, hmac, smtplib) -- no new dependencies, matching the
rest of the app. Swap in a transactional email API (Resend/Postmark/SES) for
production deliverability instead of raw SMTP if you prefer; the mint/verify
logic below doesn't change either way.
"""
import hashlib
import hmac
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mint_license  # reuses the SAME signing key + mint() the CLI tool uses

# --------------------------------------------------------------------------- #
# TODO 1: your webhook signing secret (from the processor's dashboard, after
# you create the product). NEVER commit this to git -- set it as an env var.
WEBHOOK_SECRET = os.environ.get("FRAGNETIC_WEBHOOK_SECRET", "")

# TODO 2: map the processor's plan/variant ID -> (tier, days). days=0 means
# perpetual (a one-time "lifetime" purchase); use 30/365 for monthly/annual
# subscriptions -- the buyer's key just expires and they re-subscribe/renew.
PLAN_MAP = {
    "1190540": ("pro", 30),      # Fragnetic Pro -- Monthly ($5.99)  [confirm this is monthly]
    "1861621": ("pro", 365),     # Fragnetic Pro -- Annual ($49)     [confirm this is annual]
}

# TODO 3: SMTP credentials for sending the key by email. For a real launch,
# prefer a transactional email API (Resend/Postmark/SES) over raw Gmail SMTP
# for deliverability -- but this works fine to start.
SMTP_HOST = os.environ.get("FRAGNETIC_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("FRAGNETIC_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("FRAGNETIC_SMTP_USER", "")
SMTP_PASS = os.environ.get("FRAGNETIC_SMTP_PASS", "")
FROM_ADDR = os.environ.get("FRAGNETIC_FROM_ADDR", SMTP_USER)

# TODO 4: port to listen on (make sure your host/firewall exposes this, or
# put it behind a reverse proxy with TLS -- webhooks should be HTTPS in prod).
PORT = int(os.environ.get("FRAGNETIC_WEBHOOK_PORT", "8899"))
# --------------------------------------------------------------------------- #


def verify_signature(raw_body, signature_header):
    """Lemon Squeezy signs the raw body with HMAC-SHA256 + your webhook secret,
    sent in the X-Signature header. NEVER process a webhook without this check
    -- otherwise anyone can POST a fake 'purchase' and mint themselves a key."""
    if not WEBHOOK_SECRET:
        return False
    mac = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature_header or "")


def send_key_email(to_email, key, tier, days):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        print("[!] SMTP not configured -- key NOT emailed. Key was:", key)
        return False
    period = "a %d-day" % days if days else "a lifetime"
    body = (
        "Thanks for grabbing Fragnetic %s!\n\n"
        "Your license key (%s access):\n\n%s\n\n"
        "Paste it into Fragnetic under Account > License to unlock it.\n"
        "It's verified fully offline -- no internet needed after you paste it.\n\n"
        "Keep this email; you can reuse the same key if you reinstall.\n"
    ) % (tier.title(), period, key)
    msg = MIMEText(body)
    msg["Subject"] = "Your Fragnetic %s license key" % tier.title()
    msg["From"] = FROM_ADDR
    msg["To"] = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception as e:
        print("[X] email send failed:", e)
        return False


def handle_purchase(event):
    """Extract buyer email + plan from a Lemon Squeezy 'order_created' /
    'subscription_created' event and mint+email a key. Adjust the field
    paths here if you use a different processor -- the mint/email logic
    below stays identical."""
    data = (event.get("data") or {})
    attrs = (data.get("attributes") or {})
    email = attrs.get("user_email") or attrs.get("customer_email") or ""
    name = attrs.get("user_name") or attrs.get("customer_name") or "Customer"
    variant_id = str(attrs.get("variant_id") or attrs.get("first_order_item", {}).get("variant_id") or "")
    if not email:
        print("[!] webhook had no buyer email, skipping:", json.dumps(event)[:200])
        return
    tier, days = PLAN_MAP.get(variant_id, ("pro", 30))   # fallback: 30-day Pro
    key, payload = mint_license.mint(tier=tier, name=name, email=email, days=days)
    print("[OK] minted %s key for %s (variant %s, %s)" % (tier, email, variant_id, key[:24] + "..."))
    send_key_email(email, key, tier, days)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[webhook]", fmt % args)

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        sig = self.headers.get("X-Signature", "")
        if not verify_signature(raw, sig):
            print("[X] BAD SIGNATURE -- rejecting webhook (check FRAGNETIC_WEBHOOK_SECRET)")
            self.send_response(401)
            self.end_headers()
            return
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        # only act on successful purchase/renewal events; ignore the rest
        event_name = ((event.get("meta") or {}).get("event_name") or "")
        if event_name in ("order_created", "subscription_created", "subscription_payment_success"):
            try:
                handle_purchase(event)
            except Exception as e:
                print("[X] error handling purchase:", e)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def main():
    if not WEBHOOK_SECRET:
        print("[!] FRAGNETIC_WEBHOOK_SECRET is not set -- every webhook will be "
              "rejected until you set it. See TODO 1 at the top of this file.")
    if not PLAN_MAP:
        print("[!] PLAN_MAP is empty -- every purchase will fall back to a 30-day "
              "Pro key until you fill in your real variant IDs. See TODO 2.")
    print("Fragnetic webhook receiver listening on :%d (POST /webhook)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
