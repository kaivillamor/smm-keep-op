MIN_WIN_PROB_EDGE = 0.03   # 3% edge on moneyline
MIN_TOTAL_EDGE   = 0.5    # 0.5 runs on totals


def detect_edges(probabilities: list[dict]) -> list[dict]:
    """
    Compares our model's probabilities against the book's implied probabilities.
    Returns only games where we disagree enough to have a betting edge.
    Each returned dict includes edge info and the best available bet for each side.
    """
    edges = []
    for game in probabilities:
        ml_edge, ml_side, ml_bet = _moneyline_edge(game)
        total_edge, total_side, total_bet = _total_edge(game)

        has_ml_edge    = abs(ml_edge)    >= MIN_WIN_PROB_EDGE
        has_total_edge = abs(total_edge) >= MIN_TOTAL_EDGE

        if not has_ml_edge and not has_total_edge:
            continue

        edges.append({
            **game,
            "moneyline_edge": round(ml_edge, 4),
            "moneyline_side": ml_side,        # "home" or "away"
            "moneyline_bet":  ml_bet,         # {odds, book} for best available line
            "total_edge":     round(total_edge, 4),
            "total_side":     total_side,     # "over" or "under"
            "total_bet":      total_bet,      # {odds, line, book}
            "has_ml_edge":    has_ml_edge,
            "has_total_edge": has_total_edge,
        })

    print(f"[edge_detector] {len(probabilities)} games → {len(edges)} with edge")
    return edges


def american_to_implied_prob(odds: int) -> float:
    """Converts American odds to raw implied probability (includes vig)."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def remove_vig(home_prob: float, away_prob: float) -> tuple[float, float]:
    """
    Strips the bookmaker's vig from both sides so probabilities sum to 1.0.
    Without this, we'd be comparing our clean probability to an inflated book probability.
    """
    total = home_prob + away_prob
    return home_prob / total, away_prob / total


def _moneyline_edge(game: dict) -> tuple[float, str, dict]:
    """
    Returns (edge, side, best_bet).
    Edge is positive when we think the side is more likely than the book does.
    Side is whichever team we'd bet on.
    """
    our_home_win_pct = game.get("our_home_win_pct", 0.5)
    best_lines       = game.get("raw_odds", {}).get("best_lines", {})

    home_ml = best_lines.get("home_ml")
    away_ml = best_lines.get("away_ml")

    if not home_ml or not away_ml:
        return 0.0, "", {}

    raw_home_prob = american_to_implied_prob(home_ml["odds"])
    raw_away_prob = american_to_implied_prob(away_ml["odds"])
    book_home_prob, book_away_prob = remove_vig(raw_home_prob, raw_away_prob)

    home_edge = our_home_win_pct - book_home_prob
    away_edge = (1 - our_home_win_pct) - book_away_prob

    if abs(home_edge) >= abs(away_edge):
        side = "home"
        edge = home_edge
        bet  = home_ml
    else:
        side = "away"
        edge = away_edge
        bet  = away_ml

    return edge, side, bet


def _total_edge(game: dict) -> tuple[float, str, dict]:
    """
    Returns (edge, side, best_bet).
    Edge is in runs — positive means we think the game goes OVER the book's line.
    Side is "over" or "under".
    """
    our_total  = game.get("our_implied_total")
    best_lines = game.get("raw_odds", {}).get("best_lines", {})

    over_bet  = best_lines.get("over")
    under_bet = best_lines.get("under")

    if our_total is None or not over_bet:
        return 0.0, "", {}

    book_total = over_bet.get("line")
    if book_total is None:
        return 0.0, "", {}

    run_edge = our_total - book_total   # positive = we think OVER

    if run_edge >= 0:
        return run_edge, "over", over_bet
    else:
        return run_edge, "under", under_bet or {}
