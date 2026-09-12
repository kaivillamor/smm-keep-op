"""Personal-logic hook for NBA — the place your own reads go.

Structured deliberately so the effect is MEASURABLE, which is the whole point:

  * the quant model produces `base_prob`
  * these functions return a DELTA, never a replacement
  * both get logged separately, so `compare_owner_logic()` can A/B them on identical
    outcomes as a paired test

Baseball learned this the hard way — the delta was computed but not logged, so ~1,400
graded rows can never say whether the rules helped. Do not repeat that here: log
`base_prob` and `owner_adj` from the very first prediction. See PROJECT.md §24.

Honest-use rule: write rules down, THEN measure forward. Tuning them after reading the
comparison output fits noise and the result stops being evidence.
"""


def apply_prop_owner_logic(player_name: str, market: str, opponent: str,
                           line: float, base_prob: float) -> float:
    """Player-level prop adjustment. Returns a float to ADD to base_prob.

    Positive = you like it more than the model does; negative = fade.
    Caller clamps the sum to [0, 1].

    Unlike baseball, an NBA prop needs `market` and `line` to be meaningful — "Luka over"
    is not a rule, "Luka over 8.5 assists vs a drop-coverage defence" is. Add rules as:

        if player_name == "Full Name" and market == "player_assists" and line <= 8.5:
            adjustment += 0.05
    """
    adjustment = 0.0
    # player/market rules go here
    return adjustment


def apply_owner_logic(game: dict, quant_score: float) -> float:
    """Game-level adjustment (moneyline / totals). Returns a float to ADD to the home
    win probability.

    NOTE: the baseball twin of this function has never had a single rule and returns a
    hard 0.0 — it is flagged for removal in the §25 audit. Do not keep this one unless it
    earns its place.
    """
    return 0.0
