
LEAGUE_AVG_FIP = 4.15
LEAGUE_AVG_XFIP = 4.15

# 5% win probability swing per 1.0 FIP difference between the two starters
WIN_PROB_PER_FIP_UNIT = 0.05

# Fatigue: innings pitched per start below this threshold suggests a short leash
SHORT_LEASH_IP_THRESHOLD = 5.0


def score_pitchers(game: dict, stats: dict) -> float:
    """
    Returns a home team win probability adjustment from the pitching matchup.
    Positive = home starter has an edge, negative = away starter has an edge.
    Range is roughly -0.15 to +0.15.
    """
    probable = stats.get("probable_pitchers", {})
    pitcher_stats = stats.get("pitcher_stats", {})

    game_entry = _match_game(game, probable)
    if not game_entry:
        return 0.0

    home_pid = str(game_entry.get("home_pitcher_id") or "")
    away_pid = str(game_entry.get("away_pitcher_id") or "")

    home_score = _pitcher_score(pitcher_stats.get(home_pid, {}))
    away_score = _pitcher_score(pitcher_stats.get(away_pid, {}))

    return round(home_score - away_score, 4)


def _pitcher_score(stats: dict) -> float:
    """
    Converts a pitcher's stats into a win probability contribution.
    Lower FIP/xFIP → higher score (better for their team).
    Returns delta relative to league average, in win probability units.
    """
    if not stats:
        return 0.0

    fip = stats.get("fip") or LEAGUE_AVG_FIP
    xfip = stats.get("xfip") or fip

    # xFIP weighted more heavily — normalizes HR variance, more predictive
    blended = fip * 0.35 + xfip * 0.65

    fatigue_penalty = fatigue_index(stats)

    return (LEAGUE_AVG_FIP - blended) * WIN_PROB_PER_FIP_UNIT - fatigue_penalty


def fatigue_index(stats: dict) -> float:
    """
    Returns a penalty (0.0–0.05) when a pitcher shows signs of being stretched thin.
    Uses IP/GS as a proxy for average outing length — short outings suggest command
    issues or a team keeping them on a limit.
    Note: a full per-game log would make this much more precise.
    """
    ip = stats.get("innings_pitched", 0)
    gs = stats.get("games_started", 0)

    if not gs or not ip:
        return 0.0

    avg_ip_per_start = ip / gs
    if avg_ip_per_start < SHORT_LEASH_IP_THRESHOLD:
        shortfall = SHORT_LEASH_IP_THRESHOLD - avg_ip_per_start
        return min(shortfall * 0.01, 0.05)

    return 0.0


def _match_game(game: dict, probable: dict) -> dict | None:
    home = game.get("home_team", "").lower()
    away = game.get("away_team", "").lower()

    for entry in probable.values():
        if (entry.get("home_team", "").lower() == home and
                entry.get("away_team", "").lower() == away):
            return entry
    return None
