"""
NBA prop model, v1: P(value > line) for a counting stat, read off a normal with the
player's blended season/recent mean and own sample SD. Lines sit at .5, so no continuity
correction is needed ("over 26.5" is "27 or more").
"""
import math

# v2's pre-registered inputs and the graded rows that trigger its fit, then its test
# (spec in LEVERS.md).
V2_INPUTS = ("teammate_usage_out", "days_rest", "opp_allow_factor")
V2_FIT_ROWS = 3000
V2_TEST_ROWS = 3000

RECENT_WEIGHT = 0.35        # weight on recent form; the rest goes on the season average
MIN_GAMES_FOR_SD = 5        # below this many recent games, use the market's DEFAULT_SD
DEFAULT_SD = {
    "player_points": 7.5,
    "player_rebounds": 3.2,
    "player_assists": 2.6,
    "player_made_threes": 1.5,
}


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prop_probability(form: dict, market: str, line: float | None) -> float | None:
    """P(stat > line), or None when there is not enough to say anything."""
    if line is None:
        return None
    season, recent = form.get("season_avg"), form.get("recent_avg")
    if season is None and recent is None:
        return None

    if season is None:
        mean = recent
    elif recent is None or not form.get("recent_games"):
        mean = season
    else:
        mean = (1 - RECENT_WEIGHT) * season + RECENT_WEIGHT * recent

    sd = form.get("stat_sd")
    if sd is None or sd <= 0 or (form.get("recent_games") or 0) < MIN_GAMES_FOR_SD:
        sd = DEFAULT_SD.get(market)
    if not sd or sd <= 0:
        return None

    return round(1.0 - _phi((float(line) - mean) / sd), 4)
