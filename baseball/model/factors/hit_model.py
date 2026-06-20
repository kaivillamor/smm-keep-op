LEAGUE_AVG_BA  = 0.248   # 2024 MLB batting average
LEAGUE_H_PER_9 = 8.5     # hits allowed per 9 innings, league-average pitcher
MIN_SPLIT_AB   = 30      # minimum AB before trusting a L/R split
MIN_H2H_AB     = 10      # minimum career AB vs this pitcher before applying H2H blend
MIN_RECENT_AB  = 10      # minimum AB in recent window before applying form adjustment
MIN_VENUE_AB   = 20      # minimum career AB at this venue before applying venue blend

# ── Signal weights ────────────────────────────────────────────────────────────
# Batter BA blending (applied in order — each layer adjusts from the previous):
H2H_WEIGHT         = 0.40   # career avg vs this specific pitcher replaces 40% of season split
RECENT_WEIGHT      = 0.20   # last-14d hot/cold ratio multiplier on blended BA
VENUE_WEIGHT       = 0.05   # career avg at this ballpark — very light, just a nudge
TEAM_RECENT_WEIGHT = 0.10   # team-level hot/cold multiplier (collective lineup rhythm)

# Pitcher quality blending:
PITCHER_RECENT_WEIGHT = 0.40  # last-3-starts H/9 blended with season H/9
REST_ADJ_PER_DAY      = 0.01  # ±1% quality factor per day away from 4-day normal rest

# Situational:
DAY_GAME_BA_PENALTY = 0.010  # flat BA reduction — batters hit slightly worse in day games

HIT_PARLAY_LEGS  = 8   # legs surfaced in the daily hit parlay
MAX_LINEUP_DEPTH = 6   # only score batters in positions 1–6 (most plate appearances)

# Historical expected PAs per game by batting order position.
_PA_BY_POS = {
    1: 4.6, 2: 4.4, 3: 4.3, 4: 4.1,
    5: 4.0, 6: 3.9, 7: 3.8, 8: 3.7, 9: 3.5,
}


def score_batter_hit_prob(
    batter_splits: dict,
    pitcher_stats: dict,
    pitcher_hand: str,
    lineup_pos: int,
    park_factor: float = 1.0,
    h2h_stats: dict | None = None,
    recent_ba_stats: dict | None = None,
    venue_stats: dict | None = None,
    pitcher_recent: dict | None = None,
    team_recent: dict | None = None,
    is_day_game: bool = False,
) -> float:
    """
    Returns P(batter gets 1+ hits) for one game.

    Batter BA layers (applied in order):
      1. Season BA vs pitcher handedness (L/R split)
      2. H2H career avg vs this specific pitcher   — 40% blend
      3. Recent form (L14d BA) hot/cold multiplier — 20%
      4. Venue career avg                          —  5%
      5. Team recent offensive form                — 10%
      6. Day game penalty                          — flat –0.010

    Pitcher quality factor:
      Season H/9 blended 60/40 with last-3-starts H/9.
      Days-rest deviation from 4-day norm adjusts quality factor ±1%/day.
    """
    # ── 1. Base BA from season L/R split ─────────────────────────────────────
    split_key = "vs_rhp" if pitcher_hand == "R" else "vs_lhp"
    split     = batter_splits.get(split_key, {})
    season_ba = split.get("avg")
    if not season_ba or season_ba <= 0 or split.get("ab", 0) < MIN_SPLIT_AB:
        season_ba = LEAGUE_AVG_BA
    batter_ba = season_ba

    # ── 2. H2H blend ─────────────────────────────────────────────────────────
    if h2h_stats and h2h_stats.get("ab", 0) >= MIN_H2H_AB:
        h2h_avg   = h2h_stats.get("avg") or 0.0
        batter_ba = batter_ba * (1 - H2H_WEIGHT) + h2h_avg * H2H_WEIGHT

    # ── 3. Recent form multiplier ─────────────────────────────────────────────
    if recent_ba_stats and recent_ba_stats.get("ab", 0) >= MIN_RECENT_AB:
        recent_avg = recent_ba_stats.get("avg")
        if recent_avg is not None:
            hot_cold   = recent_avg / (season_ba or LEAGUE_AVG_BA)
            batter_ba *= 1 + (hot_cold - 1) * RECENT_WEIGHT

    # ── 4. Venue blend (very light) ───────────────────────────────────────────
    if venue_stats and venue_stats.get("ab", 0) >= MIN_VENUE_AB:
        venue_avg = venue_stats.get("avg")
        if venue_avg is not None:
            batter_ba = batter_ba * (1 - VENUE_WEIGHT) + venue_avg * VENUE_WEIGHT

    # ── 5. Team recent offense multiplier ────────────────────────────────────
    if team_recent and team_recent.get("avg") is not None:
        team_ratio = team_recent["avg"] / LEAGUE_AVG_BA
        batter_ba *= 1 + (team_ratio - 1) * TEAM_RECENT_WEIGHT

    # ── 6. Day game penalty ───────────────────────────────────────────────────
    if is_day_game:
        batter_ba -= DAY_GAME_BA_PENALTY

    batter_ba = max(batter_ba, 0.0)

    # ── Pitcher quality factor ────────────────────────────────────────────────
    season_h9 = _pitcher_hits_per_9(pitcher_stats)

    if pitcher_recent and pitcher_recent.get("h_per_9") is not None:
        pitcher_h9 = (season_h9 * (1 - PITCHER_RECENT_WEIGHT)
                      + pitcher_recent["h_per_9"] * PITCHER_RECENT_WEIGHT)
    else:
        pitcher_h9 = season_h9

    quality_factor = pitcher_h9 / LEAGUE_H_PER_9

    # Days rest: 4 days is normal. Short/long rest nudges quality factor.
    if pitcher_recent and pitcher_recent.get("days_rest") is not None:
        rest_delta     = pitcher_recent["days_rest"] - 4
        quality_factor *= 1 + rest_delta * REST_ADJ_PER_DAY

    # ── Park + final calculation ──────────────────────────────────────────────
    park_adj    = 1.0 + (park_factor - 1.0) * 0.3
    expected_pa = _PA_BY_POS.get(lineup_pos, 4.0)
    adjusted_ba = min(batter_ba * quality_factor * park_adj, 0.600)

    return round(1.0 - (1.0 - adjusted_ba) ** expected_pa, 4)


def _pitcher_hits_per_9(pitcher_stats: dict) -> float:
    """H/9 = WHIP×9 − BB/9. Falls back through opp_avg, then league average."""
    whip = pitcher_stats.get("whip")
    bb9  = pitcher_stats.get("bb_per_9")
    if whip is not None and bb9 is not None:
        return max(whip * 9 - bb9, 0.5)
    if whip is not None:
        return max(whip * 9 - 3.2, 0.5)
    opp_avg = pitcher_stats.get("opp_avg")
    if opp_avg is not None:
        return max(opp_avg * 34, 0.5)
    return LEAGUE_H_PER_9
