# Fragnetic — Marketing Pack

*Draft, 2026-07-02. Grounded in the actual feature gating in
`fragroute_license.py` (FEATURES dict) — not aspirational, this is what the
app really does today.*

---

## 1. Positioning

**One-liner:**
> Fragnetic is your all-in-one FragPunk companion — better regions, better
> footage, and a local AI coach that actually watches your game.

**Elevator pitch (3 sentences):**
> Fragnetic finds your fastest FragPunk region, records and reviews your
> matches, and puts a private AI coach in your corner — all running locally on
> your PC, with nothing uploaded anywhere. No subscription to a cloud
> service, no ads, no data collection. Just a faster queue, better footage,
> and a coach that's actually watching.

**Positioning statement:**
> For FragPunk players who are tired of guessing which region to queue in and
> want real feedback on their play, Fragnetic is the companion app that
> combines region optimization, match recording, and a local AI coach —
> unlike cloud coaching tools or generic VPN apps, Fragnetic runs entirely on
> your machine and is purpose-built for one game.

### Differentiators (the "why us" in one breath each)
- **Built for FragPunk specifically**, not a generic gaming VPN or a generic
  screen recorder bolted together.
- **100% local AI** — the coach, voice, and vision models run on your PC.
  Nothing about your play gets uploaded to train someone else's product.
- **No injection, no memory reads, no input automation** — captures your
  screen the same way OBS does and reads network state the same way any app
  can; see the architecture note in the FAQ below.
- **One app, not four** — region routing + recording + AI coach + voice
  replaces separate VPN, capture, and coaching subscriptions.

---

## 2. Feature list (Free vs Pro — matches the actual code gating)

### Free
- **Smart region routing** — see live ping across every FragPunk region,
  auto-connect to your best one, and log your queue times over time.
- **Direct Region Lock** *(see pricing note below)* — force a specific
  region without a VPN.
- **Net overlay reader** — pulls FragPunk's own ping/loss numbers off-screen
  so you always know your real connection quality.
- **Weapon Locker** — an auto-organized gallery of your skins, built from
  your own screenshots.
- **Stats & queue history** — rank tracking and a full log of your queue
  times by region and time of day.
- **Shard card reference** — the full, verified card catalog in one place.

### Pro
- **AI Coach — chat & voice** — ask anything about FragPunk (weapons, cards,
  economy, strategy) and get answers grounded in real game data, not
  guesses. Talk to it hands-free with a hotkey or full voice-to-voice mode.
- **Auto-recording & highlights** — matches record automatically; an offline
  detector finds your best moments so you're not scrubbing through a full
  match to find the clip worth keeping.
- **Match video editor** — trim, caption, and export clips without leaving
  the app.
- **Session reports** — a readable summary of how a session went: regions
  played, queue health, and what stood out.
- **AI image generation** — for content creators who want quick visual
  assets from their sessions.

*(Admin tier is dev/owner-only — training tools for the underlying AI,
not part of the consumer product.)*

### ⚠️ Pricing decision needed before launch
`Direct Region Lock` (the firewall-based region-forcing feature, our newest
and arguably strongest differentiator vs. a plain VPN) is **currently
ungated** — it works on Free. Two paths:
- **Keep it free** — strengthens the free tier as a growth/acquisition hook,
  Pro sells on the AI coach + recording instead.
- **Move it to Pro** — it's more novel and higher-value than basic VPN
  routing; gating it gives Pro a second strong hook beyond the AI coach.

*Recommendation: keep it free for launch.* It's a great differentiator to
get people in the door and talking about the app; the AI coach + auto-
recording are sticky enough to carry the Pro sell on their own, and "your
free tier already beats their paid VPN app" is a strong word-of-mouth line.
Revisit if conversion is soft after launch.

---

## 3. Pricing

Reference points: ExitLag/WTFast (VPN-only, gaming-ping tools) run ~$4–7/mo;
Overwolf-ecosystem coaching apps run $5–10/mo; most are subscription-only
with no meaningful free tier. Fragnetic's free tier already outperforms a
lot of those paid VPN tools on region routing alone — that's a real edge.

**Suggested structure:**

| Tier | Price | Notes |
|---|---|---|
| **Free** | $0 | Full region routing, net overlay, locker, stats, cards — forever, no nag |
| **Pro** | **$5.99/mo** or **$49/yr** (~32% off) | AI coach, recording+highlights, video editor, reports, image gen |
| **14-day Pro trial** | Free | Already built into the licensing engine — every new account gets it automatically |

Keep the **annual plan visually anchored** next to monthly (it's the
higher-margin, lower-churn option) — e.g. "$49/yr — save $23."

*Adjust the number to your market once you have a few real trial-to-paid
data points; $5.99 is a reasonable anchor for a single-game companion tool
with local AI, not a guess pulled from nothing but also not tested against
real conversion data yet.*

---

## 4. Landing page copy

### Hero
> **Fragnetic**
> Your FragPunk companion. Better regions. Better footage. A coach that
> actually watches.
>
> [Download Free] [See Pro features]
>
> *Runs 100% locally on your PC. No account required to try it.*

### Section: "Stop guessing your region"
> FragPunk doesn't tell you which region is actually fastest — Fragnetic
> does. Live ping across every region, logged queue times, and a one-click
> way to lock onto your best connection. No VPN subscription required.

### Section: "A coach that's actually watching"
> Ask Fragnetic's AI coach anything — weapon timing, card strategy, economy
> calls — by typing or just talking. It remembers the conversation whichever
> way you ask, and every answer is grounded in real FragPunk data, not a
> guess.

### Section: "Never lose the clip"
> Matches record automatically in the background with zero FPS impact. An
> offline highlight detector finds your best moments, so you spend your time
> watching the good parts, not scrubbing a 40-minute VOD.

### Section: "Built local-first"
> Everything — your account, your recordings, your AI conversations — stays
> on your PC. No telemetry, no cloud requirement, nothing uploaded without
> you choosing to. See our Privacy Policy for the specifics.

### FAQ (pulls directly from the risk research — answer honestly, don't dodge)
**Is this safe to use? Will I get banned?**
> Fragnetic is an independent, unofficial tool — it's not made by or
> affiliated with FragPunk's developer. We built it to avoid touching the
> game's process memory, injecting code, or automating input — the same
> category of behavior anti-cheat systems actually look for. That said, no
> third-party tool can promise immunity from any game's enforcement system,
> and use is governed by FragPunk's own Terms of Service. Full disclosure in
> our EULA.

**Does this give me an unfair advantage (aimbot/ESP)?**
> No. Fragnetic does not read enemy positions, automate aim, or reveal
> anything through walls in real matches. It's a connection, recording, and
> coaching tool — not a cheat.

**What data does Fragnetic collect?**
> By default, none of it leaves your PC. See our Privacy Policy — we mean
> it literally: there's no server on our end collecting your activity.

**Do I need a powerful PC?**
> The free features are lightweight. The AI coach and recording use your
> GPU efficiently and are designed to avoid competing with the game itself
> for frame time — see system requirements on the download page.

---

## 5. Launch blurbs

### Reddit-style (r/FragPunk, be transparent it's your own tool — don't
astroturf; disclose you're the developer per subreddit self-promo rules)
> Built a free companion tool for FragPunk over the last few months —
> region ping tracking + one-click region lock, auto-recording with
> highlight detection, and a local AI coach you can talk to. Everything
> runs on your own PC, nothing's uploaded. Free tier covers region routing/
> stats/locker; there's a paid tier for the AI coach + recording if you want
> it, with a 14-day trial. Happy to answer questions — I'm the dev, so ask
> me anything about how it works under the hood.
> [link]

### Discord-style (shorter, drop in relevant servers where self-promo is OK)
> Made a FragPunk companion app — region finder + region lock (no VPN
> needed), auto-recording with AI highlight detection, and a voice AI coach.
> Free tier is solid on its own; Pro adds the coach/recording. 100% local,
> nothing uploaded. [link] — feedback welcome, still actively building it.

### X/Twitter-style (short, link, no hashtag spam)
> Shipped Fragnetic — a FragPunk companion app: region routing without a
> VPN, auto-recorded highlights, and a local AI coach you can just talk to.
> Runs entirely on your PC. Free tier included. [link]

---

## 6. What to build/decide next for launch (tracked separately)
- Sales infra: payment processor + license key delivery (next workstream).
- A short demo video/GIF for the landing page (region routing, the AI coach
  answering a spoken question, and an auto-highlight clip) — nothing sells
  this better than seeing it work.
- Pick where to launch first (r/FragPunk, FragPunk Discord, ProductHunt) —
  recommend starting in the FragPunk community itself before a general
  launch site, since the audience is pre-qualified.
