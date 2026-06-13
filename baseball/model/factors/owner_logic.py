# PLACEHOLDER — owner's personal betting criteria and rules to be defined separately.
# Each rule should return a probability adjustment (float) that modifies the base quant score.
# Examples of what might live here:
#   - Fade heavy favorites in day games on the road
#   - Weight post-travel-day performance differently
#   - Specific line movement patterns
#   - Matchup types with personal edge


def apply_owner_logic(game: dict, quant_score: float) -> float:
    adjustment = 0.0
    # rules go here
    return adjustment
