# Fragnetic — Sales & License Delivery Infrastructure

*Draft, 2026-07-02. This covers the plumbing between "someone pays" and "they
have a working license key" — wired to the existing Ed25519 licensing engine
(`mint_license.py` / `fragroute_license.py`). Account creation, bank/payout
details, and tax registration are things only YOU can do (financial/identity
actions) — this doc tells you what to set up and why, and I've written the
code for the part that's pure engineering: the automated key-delivery glue.*

## 1. Picking a payment processor

| | **Lemon Squeezy** *(recommended)* | Gumroad | Stripe (direct) |
|---|---|---|---|
| Handles global sales tax/VAT for you (merchant of record) | ✅ Yes | ✅ Yes | ❌ No — you register/collect it yourself (or add Stripe Tax, +cost) |
| Subscription support (monthly/annual, cancel, dunning) | ✅ Strong | 🟡 Weaker | ✅ Strong, but you build more of it yourself |
| Webhooks for automation | ✅ Yes | ✅ Yes | ✅ Yes |
| Fees | ~5% + 50¢ | ~10% (flat, incl. card fees) | ~2.9% + 30¢ (+ tax tool cost) |
| Setup effort | Low | Lowest | Highest (you own tax compliance) |

**Recommendation: Lemon Squeezy.** For a solo developer selling internationally
with a subscription tier, being spared sales-tax/VAT registration in every
country you get a customer from is worth more than the fee difference vs.
raw Stripe. Revisit Stripe direct later if volume grows enough to justify
hiring/using a tax tool and you want the lower fee.

**What only you can do (I can't do this for you):**
- Create the Lemon Squeezy (or chosen processor) merchant account.
- Connect your payout bank account / tax ID.
- Publish the actual product listing (name, price, description — copy is in
  `MARKETING.md`).
- Get the webhook signing secret from your dashboard after creating the
  product (needed for step 3 below).

## 2. The flow

```
Customer buys Pro  →  processor sends a webhook  →  your small server verifies
it, mints a key with the SAME engine the app already trusts (Ed25519,
offline-verifiable)  →  emails the key to the customer  →  they paste it into
Account ▸ License in the app (already built, no app changes needed).
```

This reuses 100% of the licensing engine you already have — the webhook
receiver below is the only new piece, and it's small (server-side, not part
of the app you ship to customers).

## 3. Where this runs

The webhook receiver is a tiny always-on server — **it is NOT part of the
Fragnetic app** and does not ship to customers. Cheapest options: a $5–7/mo
VPS, Render/Fly.io free-tier, or a serverless function (AWS Lambda/Cloudflare
Workers — would need a small rewrite from the stdlib script below, but the
logic is identical).

**Critical: your Ed25519 PRIVATE key (`keys/fragroute_ed25519_private.pem`)
lives on THIS server, not on your dev machine and never in the shipped app.**
Treat it like a password — if it leaks, anyone can mint themselves a free
license. Back it up somewhere safe and offline; if you ever need to rotate
it, every previously-issued key stays valid (old public key can still verify
old keys) but you'd embed a new public key in future app builds.

## 4. The webhook receiver

See `sell_webhook.py` (written alongside this doc) — a minimal, dependency-
light HTTP server matching the same "pure stdlib" style as the rest of the
app. It:
1. Receives the processor's webhook on a purchase/renewal/cancellation event.
2. **Verifies the webhook signature** (never trust an unauthenticated POST —
   anyone could otherwise mint themselves a free key by faking the request).
3. Maps the purchased plan to a tier + duration (`pro`, 30 or 365 days).
4. Calls the SAME `mint()` function `mint_license.py` uses.
5. Emails the key to the customer.

**Before this can run, fill in the 4 TODOs at the top of `sell_webhook.py`**:
your webhook signing secret, your product/variant ID → tier mapping, and
your email-sending credentials (SMTP, or a transactional email API like
Resend/Postmark — recommended over raw SMTP for deliverability).

## 5. Trial flow (already built, no new work needed)

The 14-day Pro trial is already automatic in `fragroute_license.py` — a new
account gets it without any key at all. The webhook above only fires for an
*actual purchase*, converting trial → paid.

## 6. Testing before going live

1. Use your processor's test/sandbox mode to fire a test webhook.
2. Confirm `sell_webhook.py` mints a key and the email arrives.
3. Paste the key into a test install's Account ▸ License and confirm it
   unlocks Pro (`fragroute_license.verify_key`, already covered by your
   existing offline verification — no app changes needed here).
4. Only then flip the processor to live mode.

## 7. Refunds / revocation

If you issue a refund per `REFUND.md`, there's currently no built-in
"revoke a specific key" — `fragroute_license.py` verifies offline by design
(no phone-home). Two options: (a) accept that refunded keys keep working
(low-stakes for a modestly-priced app, common for indie software), or (b)
add an optional revocation list the app checks against `ONLINE_ENDPOINT` if
you ever turn that on (already stubbed in the code, off by default). Start
with (a) — it's simpler and matches the local-first privacy story; revisit
if abuse becomes a real problem.
