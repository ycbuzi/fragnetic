## Fragnetic 20.23 — coach no longer invents lancer names

A grounding fix for the AI coach, caught while filming the demo: asked for a lancer, it
made up a name ("Buster") that doesn't exist in FragPunk. Not acceptable for a coach — so it's
now hard-grounded in the real roster.

### Fixed
- **The coach is now locked to the real 13-lancer roster** — Broker, Nitro, Hollowpoint, Jaguar,
  Chum, Corona, Serket, Pathojen, Zephyr, Spider, Kismet, Axon, Sonar. Its system prompt lists
  them explicitly and **forbids naming, inventing, or misspelling any lancer outside that list**.
  If it isn't sure which lancer you mean, it now says so instead of guessing.
- Verified live: it recommends real lancers (e.g. Nitro), and when asked about a made-up name it
  replies *"we don't have a character named that"* and lists the real roster — no more
  hallucinated names.

(The shipped lancer *skin catalog* still contains OCR-era misspellings; those only affect the
skin gallery/vision grounding and are a separate cleanup — the coach no longer relies on them.)

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
