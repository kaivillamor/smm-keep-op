MIN_EDGE = 0.03
MIN_ODDS = -150
MAX_ODDS = 250
MAX_LEGS = 4


def select_legs(edges: list[dict]) -> list[dict]:
    legs = [e for e in edges if _passes_filters(e)]
    legs.sort(key=lambda x: abs(x.get("moneyline_edge", 0)), reverse=True)
    legs = _remove_same_game_duplicates(legs)
    return legs[:MAX_LEGS]


def _passes_filters(edge: dict) -> bool:
    pass


def _remove_same_game_duplicates(legs: list[dict]) -> list[dict]:
    pass
