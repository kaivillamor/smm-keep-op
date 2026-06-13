from pipeline.stats_fetcher import fetch_batter_splits

LEAGUE_AVG_OPS = 0.720

# Win probability swing per 0.100 OPS advantage for a full lineup
WIN_PROB_PER_OPS_UNIT = 0.04

# Minimum at-bats before we trust a split; below this we fall back to overall OPS
MIN_AB_FOR_SPLIT = 30


def score_lineups(game: dict, lineups: dict, pitcher_stats: dict, probable: dict) -> float:
    """
    Returns a home team win probability adjustment based on lineup quality
    against each starter's handedness.
    Positive = home lineup has the platoon/quality edge.
    """
    game_entry = _match_game(game, probable)
    if not game_entry:
        return 0.0

    game_pk = _find_game_pk(game, lineups)
    if not game_pk:
        return 0.0

    lineup_entry = lineups[game_pk]
    if not lineup_entry.get("confirmed"):
        return 0.0

    home_lineup = lineup_entry.get("home_lineup", [])
    away_lineup = lineup_entry.get("away_lineup", [])

    home_pitcher_id = str(game_entry.get("home_pitcher_id") or "")
    away_pitcher_id = str(game_entry.get("away_pitcher_id") or "")

    home_hand = _pitcher_hand(pitcher_stats.get(home_pitcher_id, {}))
    away_hand = _pitcher_hand(pitcher_stats.get(away_pitcher_id, {}))

    # Home batters face the AWAY starter, away batters face the HOME starter
    home_ops = lineup_ops_vs_hand(home_lineup, away_hand)
    away_ops = lineup_ops_vs_hand(away_lineup, home_hand)

    ops_edge = home_ops - away_ops
    return round(ops_edge * WIN_PROB_PER_OPS_UNIT, 4)


def lineup_ops_vs_hand(lineup: list[dict], pitcher_hand: str) -> float:
    """
    Returns the average OPS of a lineup against a given pitcher handedness.
    Uses the relevant L/R split per batter when we have enough sample size.
    Falls back to league average for unknown batters.
    """
    if not lineup:
        return LEAGUE_AVG_OPS

    ops_values = []
    for player in lineup:
        batter_id = player.get("id")
        if not batter_id:
            ops_values.append(LEAGUE_AVG_OPS)
            continue

        splits = _get_splits(batter_id)
        split_key = "vs_lhp" if pitcher_hand == "L" else "vs_rhp"
        split = splits.get(split_key, {})

        if split.get("ab", 0) >= MIN_AB_FOR_SPLIT and split.get("ops"):
            ops_values.append(split["ops"])
        else:
            ops_values.append(LEAGUE_AVG_OPS)

    return sum(ops_values) / len(ops_values)


def _get_splits(batter_id: int) -> dict:
    try:
        return fetch_batter_splits(batter_id)
    except Exception:
        return {}


def _pitcher_hand(stats: dict) -> str:
    return stats.get("pitch_hand", "R")


def _match_game(game: dict, probable: dict) -> dict | None:
    home = game.get("home_team", "").lower()
    away = game.get("away_team", "").lower()
    for entry in probable.values():
        if (entry.get("home_team", "").lower() == home and
                entry.get("away_team", "").lower() == away):
            return entry
    return None


def _find_game_pk(game: dict, lineups: dict) -> str | None:
    home = game.get("home_team", "").lower()
    for pk, entry in lineups.items():
        if entry.get("home_team", "").lower() == home:
            return pk
    return None
