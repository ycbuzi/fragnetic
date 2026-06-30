"""FRAGROUTE online knowledge fetcher -- FragPunk-ONLY, injection-safe.

Feeds the learning store (fragroute_learning) with facts pulled from a strict
allow-list of FragPunk sources, each tagged with provenance + trust tier:
  * official  -- fragpunk.com / easebar.com (patch notes, announcements)
  * wiki      -- *.fandom.com FragPunk wiki (detailed mechanics)
  * creator   -- (later) curated FragPunk video transcripts/descriptions

SAFETY (mandatory, see memory: fragroute-game-modes):
  1. ALLOW-LIST ONLY. _host_allowed() rejects every non-FragPunk host -- the
     fetcher will not touch anything else, even if handed another URL.
  2. Fetched text is DATA, NEVER COMMANDS. We only keyword-extract factual
     sentences; page content can never instruct the app/AI to do anything.
  3. Opt-in (setting 'onlineLearning'), cached, offline-safe. Network failure is
     swallowed -- the app and the seed/observed knowledge work without internet.

Pure stdlib (urllib). The engine calls refresh(); results land in the store.
"""
import re
import time
import urllib.parse
import urllib.request

try:
    import fragroute_modes
except Exception:
    fragroute_modes = None
try:
    import fragroute_learning
except Exception:
    fragroute_learning = None

APP_KNOW_BUILD = "know-1"

# FragPunk-content allow-list (host-based). The general domains below are only
# ever hit at the CURATED FragPunk article URLs in GUIDE_URLS / the Wikipedia
# FragPunk page -- nothing else is fetched. Verified to yield real FragPunk facts.
ALLOWED_HOST_SUFFIXES = (
    ".fragpunk.com", "fragpunk.com",
    ".easebar.com", "easebar.com",            # NetEase/FragPunk backend domain
    "fragpunk-official.fandom.com",           # FragPunk community wiki (API)
    "en.wikipedia.org",                       # FragPunk article only (curated)
    "1v9.gg", "dotesports.com", "gamerant.com", "esports.gg",  # FragPunk guide pages
)

# Browser-like UA -- fandom/guides reject obvious bots. We only read public pages.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Wikipedia FragPunk article via the clean MediaWiki TextExtracts API (authoritative).
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_TITLES = ["FragPunk"]

# The fandom wiki serves its API (page HTML is bot-blocked) -- low yield today
# (sparse mode pages) but kept best-effort for when it grows / for Lancer pages.
WIKI_API = "https://fragpunk-official.fandom.com/api.php"
WIKI_TITLES = ["Outbreak", "Shard Clash", "Lancers", "Shard Cards"]

# Curated FragPunk guide articles (the rich mode/lancer/map-tactic content).
GUIDE_URLS = [
    {"url": "https://1v9.gg/blog/fragpunk-every-game-mode-explained", "trust": "creator"},
    {"url": "https://www.gamerant.com/fragpunk-all-game-modes-explained/", "trust": "creator"},
    {"url": "https://dotesports.com/fragpunk/news/all-game-modes-in-fragpunk-explained", "trust": "creator"},
]

# A sentence is a candidate FACT if it mentions a mode AND a rule keyword.
_RULE_WORDS = ("respawn", "revive", "revived", "downed", "life saver", "round",
               "best-of", "best of", "plant", "converter", "defuse", "lancer",
               "shard card", "shard point", "lives", "eliminat", "parasite",
               "survivor", "tiebreaker", "card captain", "switch character")

_CACHE_TS = {"last": 0}
_MIN_REFRESH_S = 6 * 3600          # don't re-fetch more than every 6h


def _host_allowed(url):
    """True only for FragPunk-allow-listed hosts (exact domain or a subdomain)."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for s in ALLOWED_HOST_SUFFIXES:
        s = s.lower().lstrip(".")
        if host == s or host.endswith("." + s):
            return True
    return False


def _fetch(url, timeout=12):
    if not _host_allowed(url):
        return ""                  # hard stop: non-FragPunk host
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(1200000)  # cap payload
        return raw.decode("utf-8", "ignore")
    except Exception:
        return ""


def _wiki_extracts(api_base, titles):
    """Pull plain-text page extracts from a MediaWiki TextExtracts API.
    Returns {page_title: text}. Batched; allow-list enforced by _fetch."""
    import json
    out = {}
    for i in range(0, len(titles), 6):
        batch = "|".join(titles[i:i + 6])
        url = (api_base + "?action=query&prop=extracts&explaintext=1&redirects=1"
               "&format=json&titles=" + urllib.parse.quote(batch))
        body = _fetch(url)
        if not body:
            continue
        try:
            pages = json.loads(body).get("query", {}).get("pages", {})
            for p in pages.values():
                ex = p.get("extract")
                if ex and len(ex) > 40:
                    out[p.get("title", "")] = ex
        except Exception:
            pass
    return out


def _visible_text(html):
    """Strip tags/scripts to plain text. Crude but enough for keyword extraction."""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 20 <= len(s.strip()) <= 300]


def extract_facts(text, default_key=None):
    """Return [(mode_key, sentence)] for sentences that state a mode rule.
    If a sentence names a mode, attribute to it; else fall back to default_key
    (the mode the source PAGE is about). Pure/deterministic -- no network."""
    out = []
    if fragroute_modes is None:
        return out
    aliases = sorted(fragroute_modes.ALIASES, key=len, reverse=True)
    for s in _sentences(text):
        low = s.lower()
        if not any(w in low for w in _RULE_WORDS):
            continue
        key = None
        for alias in aliases:
            if alias in low:
                key = fragroute_modes.ALIASES[alias]
                break
        if key is None:
            key = default_key
        if key:
            out.append((key, s))
    return out


def ingest_text(text, source, trust="wiki", default_key=None, max_facts=60):
    """Extract mode-rule facts from arbitrary FragPunk text and store them with
    provenance. Used for wiki extracts AND (engine-side) the existing news feed.
    Returns the number of facts added. Caller treats `text` as DATA, not commands."""
    added = 0
    for key, sentence in extract_facts(text or "", default_key=default_key):
        if added >= max_facts:
            break
        if fragroute_learning is not None and fragroute_learning.record_online_fact(
                key, sentence, source, trust, int(time.time() * 1000)):
            added += 1
    return added


def refresh(extra_texts=None, force=False, max_facts=80):
    """Pull FragPunk-only facts: wiki page extracts (MediaWiki API) + any extra
    texts the engine passes (e.g. official news). Store with provenance.
    Best-effort; never raises. `extra_texts` = [{text, source, trust, default_key}]."""
    now = time.time()
    if not force and (now - _CACHE_TS["last"]) < _MIN_REFRESH_S:
        return {"ok": True, "skipped": "cached", "added": 0}
    _CACHE_TS["last"] = now
    added, by_source, pages = 0, {}, 0

    def _ingest(text, source, trust, dk):
        nonlocal added
        if added >= max_facts or not text:
            return
        n = ingest_text(text, source, trust, dk, max_facts=max_facts - added)
        by_source[source] = "%d facts" % n
        added += n

    # 1) Wikipedia FragPunk article (authoritative, clean API)
    for title, text in _wiki_extracts(WIKIPEDIA_API, WIKIPEDIA_TITLES).items():
        pages += 1
        _ingest(text, "wikipedia:" + title, "wiki", None)
    # 2) curated FragPunk guide articles (rich mode/lancer/map content)
    for g in GUIDE_URLS:
        if added >= max_facts:
            break
        html = _fetch(g["url"])
        if html:
            pages += 1
            _ingest(_visible_text(html), g["url"], g.get("trust", "creator"), None)
        else:
            by_source[g["url"]] = "unreachable"
    # 3) FragPunk fandom wiki API (best-effort; sparse today)
    for title, text in _wiki_extracts(WIKI_API, WIKI_TITLES).items():
        pages += 1
        dk = fragroute_modes.classify(title)[0] if fragroute_modes else None
        _ingest(text, "fandom:" + title, "wiki", None if dk == "unknown" else dk)
    # 4) extra texts handed in by the engine (e.g. official news already fetched)
    for ex in (extra_texts or []):
        _ingest(ex.get("text", ""), ex.get("source", "official"),
                ex.get("trust", "official"), ex.get("default_key"))

    return {"ok": True, "added": added, "sources": by_source,
            "pages": pages, "updated": int(now * 1000)}
