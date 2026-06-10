MIN_WIN_PROB_EDGE = 0.03
MIN_TOTAL_EDGE = 0.5


def detect_edges(probabilities: list[dict]) -> list[dict]:
    edges = []
    for game in probabilities:
        ml_edge = _moneyline_edge(game)
        total_edge = _total_edge(game)

        if abs(ml_edge) >= MIN_WIN_PROB_EDGE or abs(total_edge) >= MIN_TOTAL_EDGE:
            edges.append({**game, "moneyline_edge": ml_edge, "total_edge": total_edge})
    return edges


def american_to_implied_prob(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def _moneyline_edge(game: dict) -> float:
    pass


def _total_edge(game: dict) -> float:
    pass
