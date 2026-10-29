"""Owner rules for NBA props: a delta on the quant model's base_prob, logged separately
as owner_adj."""


def apply_prop_owner_logic(player_name: str, market: str, opponent: str,
                           line: float, base_prob: float) -> float:
    """A float to ADD to base_prob (positive = like it more, negative = fade); the caller
    clamps the sum to [0, 1]. Add rules as:

        if player_name == "Full Name" and market == "player_assists" and line <= 8.5:
            adjustment += 0.05
    """
    adjustment = 0.0
    # player/market rules go here
    return adjustment


def apply_owner_logic(game: dict, quant_score: float) -> float:
    """Game-level adjustment (moneyline / totals). Returns a float to ADD to the home
    win probability."""
    return 0.0
