def apply_owner_logic(game: dict, quant_score: float) -> float:
    """Game-level adjustment (moneylines / totals): a float to add to the home win
    probability."""
    adjustment = 0.0
    # game-level rules go here
    return adjustment


def apply_hit_owner_logic(batter_name: str, opponent_team: str, base_prob: float) -> float:
    """
    Owner rules for one batter: a float to ADD to base_prob (positive = boost, negative =
    fade); hit_pipeline clamps the sum to [0, 1]. Add rules as:

        if batter_name == "Full Name" and opponent_team == "Full Team Name":
            adjustment += 0.XX
    """
    adjustment = 0.0

    if batter_name == "Matt Olson" and opponent_team == "New York Mets":
        adjustment += 0.05

    return adjustment
