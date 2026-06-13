def build_parlay(legs: list[dict]) -> dict:
    if not legs:
        return {}

    combined_odds = _calculate_combined_odds(legs)
    total_edge    = sum(abs(l["edge"]) for l in legs)

    return {
        "legs":          legs,
        "num_legs":      len(legs),
        "combined_odds": combined_odds,
        "total_edge":    round(total_edge, 4),
    }


def _calculate_combined_odds(legs: list[dict]) -> int:
    """
    Converts each leg to decimal odds, multiplies them together,
    then converts the result back to American odds.
    """
    decimal = 1.0
    for leg in legs:
        decimal *= _american_to_decimal(leg["odds"])

    return _decimal_to_american(decimal)


def _american_to_decimal(odds: int) -> float:
    if odds > 0:
        return (odds / 100) + 1.0
    else:
        return (100 / abs(odds)) + 1.0


def _decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return round((decimal - 1) * 100)
    else:
        return round(-100 / (decimal - 1))
