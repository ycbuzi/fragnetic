# Fragnetic — Privacy Policy

*Draft — last updated 2026-07-02. Not a substitute for legal review before
launch, especially if you sell to customers in the EU/UK (GDPR) or California
(CCPA) — see the notes at the bottom.*

## The short version

Fragnetic is a **local-first** application. By default, everything it does —
your account, your license, your recordings, your AI conversations, your
settings — stays on your own computer. We do not run a server that collects
your activity, and there is no analytics/telemetry call anywhere in the app.

## What Fragnetic stores (all local, on your device)

- **Account**: a username and a password hash (PBKDF2-SHA256, not your raw
  password), stored in a local file on your machine.
- **License**: your license key and its embedded entitlement (verified with
  cryptographic signature checking — no phone-home required).
- **Recordings**: match video/audio you capture, saved to a local folder you
  control, with an auto-cleanup size limit you set.
- **AI conversations**: your chat/voice questions and the coach's answers,
  kept locally so it can hold context; you can clear this at any time.
- **App settings & logs**: queue times, region history, hardware info, and a
  diagnostic log — all local, used only to power the app's own features
  (e.g. recommending a region).

**None of the above is transmitted anywhere by default.** There is no cloud
account requirement and no built-in telemetry.

## What *can* leave your device (and when)

- **Server-region lookup**: Fragnetic reads the IP address of the FragPunk
  server you connect to (from your own network connection, the same
  information any app on your PC could see) and looks up its rough
  city/region using an **offline** database bundled with the app. This
  lookup does not go out to the internet.
- **License activation** (optional, off by default): if we ever run an
  optional online activation service, only a hashed key ID and a machine
  identifier are sent — never your gameplay data, recordings, or chat
  content. This is disabled unless explicitly configured.
- **Cloud account sync** (optional, off by default): Fragnetic supports an
  optional cloud account mode for cross-device sync. It is **not enabled** in
  the default consumer build. If enabled, only your account credentials
  (hashed) sync — never recordings or AI conversations.
- **Model downloads**: on first run, Fragnetic downloads AI models and a
  GeoIP database from public sources (Hugging Face, GitHub, DB-IP) over
  HTTPS. This is a one-time file download, not a data upload.

## What Fragnetic does NOT do

- No advertising or ad tracking.
- No selling or sharing of your data with third parties — we don't have a
  server that holds your data to begin with.
- No reading of files outside what you configure (recordings folder, app
  settings) or the FragPunk process's own network connections.
- No access to your other applications, browser history, or personal files.

## Microphone and screen recording

Voice features access your microphone only when you actively trigger them
(hotkey or the on-screen toggle) or during an active voice conversation you
started. Screen/audio recording only runs when you turn it on (manually or
via auto-record-matches). All captured media is written to your local
recordings folder; nothing is uploaded.

## Data retention & deletion

Everything Fragnetic stores lives in plain files on your computer. You can
delete your account, recordings, or AI conversation history at any time from
within the app, or by deleting the app's data folder directly. Uninstalling
the app does not delete your recordings folder by default (so you don't lose
footage by accident); you can delete it manually.

## Children's privacy

Fragnetic is not directed at children under 13 (or the relevant age in your
region) and we do not knowingly collect personal information from them,
consistent with the fact that we do not collect personal information from
anyone by default.

## Changes to this policy

If a future version adds any new data collection, we will update this
document and highlight the change in the app's release notes before it takes
effect.

## Contact

*[Fill in: support/privacy contact email.]*

---
### Notes for legal review (not shown to customers)
- If you sell into the EU/UK, GDPR requires a lawful basis, a named data
  controller, and (if any personal data does leave the device — e.g. license
  activation) a data processing addendum. Confirm with counsel whether the
  offline-by-default design meaningfully reduces this burden for your case.
- If you sell into California, CCPA/CPRA disclosures may apply once revenue/
  volume thresholds are met — track this as you scale.
- If/when you turn on optional cloud accounts or online license activation
  for real customers, this document must be updated BEFORE that goes live,
  not after.
