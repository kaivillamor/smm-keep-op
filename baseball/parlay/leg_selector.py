MIN_EDGE  = 0.03    # minimum edge to qualify a leg
MIN_ODDS  = -150    # no heavier favorites than -150
MAX_ODDS  = 250     # no bigger dogs than +250
MAX_LEGS  = 4


def select_legs(edges: list[dict]) -> list[dict]:
    """
    Expands edge-detected games into individual bet legs, filters and ranks them,
    then returns the top MAX_LEGS non-correlated legs.
    """
    legs = []
    for game in edges:
        if game.get("has_ml_edge"):
            leg = _build_ml_leg(game)
            if leg and _passes_filters(leg):
                legs.append(leg)

        if game.get("has_total_edge"):
            leg = _build_total_leg(game)
            if leg and _passes_filters(leg):
                legs.append(leg)

    legs.sort(key=lambda x: abs(x["edge"]), reverse=True)
    legs = _remove_correlated(legs)
    legs = _remove_same_game_duplicates(legs)

    print(f"[leg_selector] {len(edges)} edge games → {len(legs)} qualifying legs")
    return legs[:MAX_LEGS]


def _build_ml_leg(game: dict) -> dict | None:
    side = game.get("moneyline_side")
    bet  = game.get("moneyline_bet", {})
    if not side or not bet.get("odds"):
        return None

    team = game["home_team"] if side == "home" else game["away_team"]
    return {
        "game_id":   game["game_id"],
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "bet_type":  "ml",
        "side":      side,
        "team":      team,
        "edge":      game["moneyline_edge"],
        "odds":      bet["odds"],
        "book":      bet.get("book", ""),
        "line":      None,
        "display":   f"{_abbr(team)} ML ({_fmt_odds(bet['odds'])})",
    }


def _build_total_leg(game: dict) -> dict | None:
    side = game.get("total_side")
    bet  = game.get("total_bet", {})
    if not side or not bet.get("odds"):
        return None

    matchup = f"{_abbr(game['away_team'])}/{_abbr(game['home_team'])}"
    line    = bet.get("line", "")
    return {
        "game_id":   game["game_id"],
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "bet_type":  "total",
        "side":      side,
        "team":      None,
        "edge":      game["total_edge"],
        "odds":      bet["odds"],
        "book":      bet.get("book", ""),
        "line":      line,
        "display":   f"{matchup} {side.upper()} {line} ({_fmt_odds(bet['odds'])})",
    }


def _passes_filters(leg: dict) -> bool:
    edge = abs(leg.get("edge", 0))
    odds = leg.get("odds", 0)
    return edge >= MIN_EDGE and MIN_ODDS <= odds <= MAX_ODDS


def _remove_correlated(legs: list[dict]) -> list[dict]:
    """
    Drops correlated same-game legs: ML on a team + OVER on that same game.
    When your ML team wins big, the OVER hits too — the legs aren't independent.
    """
    keep = []
    for leg in legs:
        if _is_correlated_with_existing(leg, keep):
            continue
        keep.append(leg)
    return keep


def _is_correlated_with_existing(leg: dict, existing: list[dict]) -> bool:
    for other in existing:
        if other["game_id"] != leg["game_id"]:
            continue
        # ML + OVER on same game is correlated
        types = {leg["bet_type"], other["bet_type"]}
        if types == {"ml", "total"}:
            ml_leg    = leg    if leg["bet_type"] == "ml"    else other
            total_leg = leg    if leg["bet_type"] == "total" else other
            if ml_leg["side"] in ("home", "away") and total_leg["side"] == "over":
                return True
    return False


def _remove_same_game_duplicates(legs: list[dict]) -> list[dict]:
    """Keep only the single best leg per game (already sorted by edge desc)."""
    seen_games = set()
    keep = []
    for leg in legs:
        if leg["game_id"] not in seen_games:
            seen_games.add(leg["game_id"])
            keep.append(leg)
    return keep


def _abbr(team_name: str) -> str:
    """Returns last word of team name as a short label (e.g. 'Yankees' → 'NYY' fallback)."""
    parts = team_name.split()
    return parts[-1] if parts else team_name


def _fmt_odds(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)
