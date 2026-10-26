"""
NBA prop model — v1 baseline. P(value > line) for a counting stat.

DELIBERATELY SIMPLE, and labelled v1 because it will be replaced. Its job today is not to
be right; it is to produce a logged, graded number so the calibration harness has
something to measure from night one. MLB's binding constraint was never the model — it
was having graded rows to measure one with (PROJECT.md §29).

Method: blend the player's season and recent averages, then read P(X > line) off a normal
with that mean and the player's own sample SD. The line always sits at .5, so no
continuity correction is needed — "over 26.5" is exactly "27 or more".

Known limits, written down so they are not rediscovered as surprises:
  * A normal is wrong in the tail for a counting stat, and points come in 2s and 3s so it
    is lumpy even near the mean. It is adequate for ranking, which is what the
    ranking-gap test measures.
  * No opponent adjustment. `opp_allow_factor` (what the opponent allows in this market)
    is logged but unused here, as are injury context and vs-opponent history — logged
    first, used later, never the other way round.
  * No minutes projection. The biggest single driver of an NBA prop is tonight's minutes,
    and we only know the player's recent average, not whether he is on a minutes limit.
  * On opening night the averages come from LAST season (see stats_fetcher.pick_season),
    which is why the season label is logged on every row.
"""
import math

# Weight on recent form, with the rest on the season average. Not fitted — there is no
# data to fit it on yet. The weight-fitting experiment is exactly what the logged inputs
# exist to enable later.
RECENT_WEIGHT = 0.35

# Below this many games the player's own SD is too noisy to trust, so the blend falls back
# to a market-level default rather than a figure computed from three games.
MIN_GAMES_FOR_SD = 5
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
    """P(stat > line). None when there is not enough to say anything.

    None rather than a default: a row with no basis for a number must not carry one that
    looks like a prediction.
    """
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
