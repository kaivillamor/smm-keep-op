def apply_owner_logic(game: dict, quant_score: float) -> float:
    """
    Game-level win probability adjustments (moneylines / totals).
    Returns a float to add to the home win probability.
    Add rules below — each rule should += or -= a small float (e.g. 0.03).
    """
    adjustment = 0.0
    # game-level rules go here
    return adjustment


def apply_hit_owner_logic(batter_name: str, opponent_team: str, base_prob: float) -> float:
    """
    Batter-specific hit probability adjustments based on personal pattern recognition.
    Returns a float to add to base_prob (positive = boost, negative = fade).
    Result is clamped to [0, 1] in hit_pipeline before use.

    Add rules in the form:
        if batter_name == "Full Name" and opponent_team == "Full Team Name":
            adjustment += 0.XX
    """
    adjustment = 0.0

    # Olson historically torches Mets pitching — bump hit probability
    if batter_name == "Matt Olson" and opponent_team == "New York Mets":
        adjustment += 0.05

    return adjustment
