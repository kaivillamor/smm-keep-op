def build_parlay(legs: list[dict]) -> dict:
    if not legs:
        return {}
    return {
        "legs": legs,
        "combined_odds": _calculate_combined_odds(legs),
        "total_edge": sum(abs(l.get("moneyline_edge", 0)) for l in legs),
        "num_legs": len(legs),
    }


def _calculate_combined_odds(legs: list[dict]) -> int:
    pass
