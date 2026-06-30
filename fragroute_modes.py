"""FragPunk game-mode profiles -- makes detection, auto-clip and the AI coach
MODE-AWARE so we never mislabel events (e.g. a revivable 'downed' in Shard Clash
is NOT a death; a death in Team Deathmatch is routine, not 'you're out').

Researched facts (sources in memory: fragroute-game-modes):
 - SHARD CLASH (main competitive): round-based attack/defend -- attackers plant
   the 'Converter' at a site, defenders stop/defuse. Best-of-7 (best-of-11 in
   Ranked); a 3-3 series goes to a 1v1 tiebreaker. SINGLE LIFE per round, BUT the
   'Life Saver' shard card can REVIVE a downed teammate (revived at 30% HP) -->
   'downed' can be reversed, so downed != eliminated. Lancer may be changed in
   the PREP phase, but LOCKS once you use a skill that round.
 - OUTBREAK: asymmetric survivors vs parasites, RESPAWNS; parasites can spend
   Shard Points to SWITCH character (effective next respawn). Separate card pools.
 - TEAM DEATHMATCH / FREE-FOR-ALL / SCRIMMAGE: RESPAWN (Scrimmage = instant).
 - DUEL MASTER: 1v1, finite LIVES (lose one per defeat).
 - Other arcade: Capture the Core, One Shot, Sniper Deathmatch, Mirror Clash,
   Chaos Clash, Glunite Grab (treated as respawn/score variants by default).

Pure data + helpers, no imports. Both fragroute.py (events/auto-clip) and
fragroute_ai.py (coach) import this.
"""

# Profile fields:
#   round_based     : has prep -> active -> round-end structure (clip round-deciders)
#   single_life     : one life per round (death removes you until next round)
#   respawns        : deaths recur within a round/match (death is routine)
#   revive_possible : a 'downed' state can be reversed (don't call it a death yet)
#   lives           : finite lives across the match (Duel Master)
#   lancer_switch   : 'prep'  -> changeable during prep, locks on skill use
#                     'respawn' -> changeable, applies next respawn (Outbreak)
#                     'locked' -> fixed once the match starts
#   team            : '5v5' | '1v1' | 'ffa' | 'asym'
#   desc            : one-line human description for the coach
_DEFAULT = {
    "round_based": False, "single_life": False, "respawns": True,
    "revive_possible": False, "lives": False, "lancer_switch": "prep",
    "team": "ffa", "desc": "",
}

PROFILES = {
    "shard_clash": {
        "round_based": True, "single_life": True, "respawns": False,
        "revive_possible": True, "lives": False, "lancer_switch": "prep",
        "team": "5v5",
        "desc": "Round-based attack/defend (plant the Converter). One life per "
                "round, but the Life Saver card can revive downed teammates. "
                "Best-of-7 (Bo11 ranked); 3-3 goes to a 1v1 tiebreaker.",
    },
    "outbreak": {
        "round_based": False, "single_life": False, "respawns": True,
        "revive_possible": False, "lives": False, "lancer_switch": "respawn",
        "team": "asym",
        "desc": "Asymmetric survivors vs parasites with respawns. Parasites can "
                "spend Shard Points to switch character (next respawn). Separate "
                "card pools per side.",
    },
    "team_deathmatch": {
        "round_based": False, "single_life": False, "respawns": True,
        "revive_possible": False, "lives": False, "lancer_switch": "prep",
        "team": "5v5", "desc": "Respawn-based team kills race to a score.",
    },
    "free_for_all": {
        "round_based": False, "single_life": False, "respawns": True,
        "revive_possible": False, "lives": False, "lancer_switch": "prep",
        "team": "ffa", "desc": "Everyone for themselves; respawn and frag to a score.",
    },
    "scrimmage": {
        "round_based": False, "single_life": False, "respawns": True,
        "revive_possible": False, "lives": False, "lancer_switch": "free",
        "team": "ffa", "desc": "No-rules warm-up deathmatch with INSTANT respawns.",
    },
    "duel_master": {
        "round_based": True, "single_life": True, "respawns": False,
        "revive_possible": False, "lives": True, "lancer_switch": "prep",
        "team": "1v1", "desc": "1v1 elimination; you lose a life on each defeat.",
    },
    "capture_the_core": {
        "round_based": False, "single_life": False, "respawns": True,
        "revive_possible": False, "lives": False, "lancer_switch": "prep",
        "team": "5v5", "desc": "Objective mode: secure/carry the core. Respawns.",
    },
    "sniper_deathmatch": {
        "round_based": False, "single_life": False, "respawns": True,
        "revive_possible": False, "lives": False, "lancer_switch": "prep",
        "team": "ffa", "desc": "Snipers-only respawn deathmatch.",
    },
    "one_shot": {
        "round_based": False, "single_life": False, "respawns": True,
        "revive_possible": False, "lives": False, "lancer_switch": "prep",
        "team": "ffa", "desc": "One-shot-kill respawn deathmatch.",
    },
}

# OCR'd / display name (lowercased) -> profile key. Substring-matched, so partial
# or noisy OCR ('shard clash - ranked') still resolves.
ALIASES = {
    "shard clash": "shard_clash", "shard": "shard_clash", "ranked": "shard_clash",
    "advanced standard": "shard_clash", "standard": "shard_clash",
    "outbreak": "outbreak", "parasite": "outbreak", "survivor": "outbreak",
    "team deathmatch": "team_deathmatch", "tdm": "team_deathmatch",
    "free-for-all": "free_for_all", "free for all": "free_for_all", "ffa": "free_for_all",
    "scrimmage": "scrimmage", "scrim": "scrimmage", "training": "scrimmage",
    "duel master": "duel_master", "duel": "duel_master",
    "capture the core": "capture_the_core", "capture": "capture_the_core",
    "sniper deathmatch": "sniper_deathmatch", "sniper": "sniper_deathmatch",
    "one shot": "one_shot", "mirror clash": "shard_clash", "chaos clash": "shard_clash",
    "glunite grab": "capture_the_core",
}


def profile_for(key):
    """Return a full profile dict for a mode key (defaults filled in)."""
    p = dict(_DEFAULT)
    p.update(PROFILES.get(key, {}))
    p["key"] = key
    return p


def classify(mode_name):
    """Map a raw/OCR'd mode name to (key, profile). Unknown -> ('unknown', default
    with respawns=True) so a mislabeled death is treated as routine, not 'out'."""
    n = (mode_name or "").strip().lower()
    if n:
        # longest alias first so 'team deathmatch' beats 'deathmatch'
        for alias in sorted(ALIASES, key=len, reverse=True):
            if alias in n:
                k = ALIASES[alias]
                return k, profile_for(k)
    return "unknown", profile_for("unknown")


def death_is_terminal(key):
    """True if a death removes you from play for the rest of the round (so it's a
    round-deciding moment worth clipping), False if you'll respawn."""
    p = profile_for(key)
    return bool(p["single_life"]) and not bool(p["respawns"])


def interpret_down(key, revived_within_s=None, revive_window_s=8):
    """Turn a raw 'downed' detection into a logical event for this mode.
    Returns 'eliminated' | 'downed' | 'death'(respawn modes).
      * revive-capable single-life modes: 'downed' until the revive window passes
        with no revive -> 'eliminated'.
      * respawn modes: always 'death' (routine, you'll respawn)."""
    p = profile_for(key)
    if p["respawns"]:
        return "death"
    if p["revive_possible"]:
        if revived_within_s is not None and revived_within_s <= revive_window_s:
            return "downed"          # was revived -> not a kill against you
        return "eliminated"
    return "eliminated"


def all_modes():
    return {k: profile_for(k) for k in PROFILES}
