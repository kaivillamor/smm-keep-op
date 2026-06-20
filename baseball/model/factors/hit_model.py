LEAGUE_AVG_BA  = 0.248   # 2024 MLB batting average
LEAGUE_H_PER_9 = 8.5     # hits allowed per 9 innings, league-average pitcher
MIN_SPLIT_AB   = 30      # minimum AB before trusting a L/R split

HIT_PARLAY_LEGS   = 4    # legs surfaced in the daily hit parlay
MAX_LINEUP_DEPTH  = 6    # only score batters in positions 1-6 (most plate appearances)

# Historical expected PAs per game by batting order position.
# Top of order accumulates more PA over a 9-inning game.
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
) -> float:
    """
    Returns P(batter gets 1+ hits) for one game.

    batter_splits : from fetch_batter_splits() — {vs_rhp: {avg, ab, ...}, vs_lhp: {...}}
    pitcher_stats : from pitcher_stats[pid] — whip, bb_per_9, opp_avg
    pitcher_hand  : "R" or "L"
    lineup_pos    : batting order position 1–9
    park_factor   : runs_factor from park_factors.py (1.0 = neutral)
    """
    split_key = "vs_rhp" if pitcher_hand == "R" else "vs_lhp"
    split = batter_splits.get(split_key, {})
    batter_ba = split.get("avg")
    ab = split.get("ab", 0)

    if not batter_ba or batter_ba <= 0 or ab < MIN_SPLIT_AB:
        batter_ba = LEAGUE_AVG_BA

    expected_pa = _PA_BY_POS.get(lineup_pos, 4.0)
    pitcher_h9 = _pitcher_hits_per_9(pitcher_stats)

    # How much better/worse than a league-avg pitcher at allowing hits
    quality_factor = pitcher_h9 / LEAGUE_H_PER_9

    # Park runs factor mildly affects contact environment (0.3 dampening — park affects
    # run totals more than raw contact rates)
    park_adj = 1.0 + (park_factor - 1.0) * 0.3

    adjusted_ba = min(batter_ba * quality_factor * park_adj, 0.600)

    # P(1+ hits in N PA) = 1 - P(0 hits)^expected_pa
    prob = 1.0 - (1.0 - adjusted_ba) ** expected_pa
    return round(prob, 4)


def _pitcher_hits_per_9(pitcher_stats: dict) -> float:
    """
    Derive H/9 from available pitcher stats.

    WHIP = (BB + H) / IP  →  H/9 = WHIP×9 - BB/9
    opp_avg can serve as a secondary signal when WHIP is unavailable.
    """
    whip = pitcher_stats.get("whip")
    bb9  = pitcher_stats.get("bb_per_9")

    if whip is not None and bb9 is not None:
        return max(whip * 9 - bb9, 0.5)
    if whip is not None:
        return max(whip * 9 - 3.2, 0.5)   # 3.2 ≈ league-avg BB/9

    # Fall back to opp_avg as a rough proxy: .250 opp_avg ≈ 8.5 H/9
    opp_avg = pitcher_stats.get("opp_avg")
    if opp_avg is not None:
        return max(opp_avg * 34, 0.5)      # 34 ≈ AB/9 for a typical starter

    return LEAGUE_H_PER_9
