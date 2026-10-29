def combine_odds(american_odds: list[int]) -> int | None:
    """Combine a list of American odds into a single parlay price.
    Returns None if any leg's odds are missing (can't price the parlay)."""
    if not american_odds or any(o is None for o in american_odds):
        return None
    dec = 1.0
    for o in american_odds:
        dec *= _american_to_decimal(o)
    return _decimal_to_american(dec)


def recommended_stake(combined_odds: int, base_stake: float = 25.0,
                      profit_floor: float = 25.0, max_stake: float = 50.0) -> float:
    """"Double-up, else $25 floor" staking, capped at max_stake: a plus-money parlay
    stakes the base unit, a minus-money one enough to clear the profit floor."""
    m = _american_to_decimal(combined_odds) - 1.0   # profit per $1 staked
    stake = base_stake if m >= 1.0 else profit_floor / m
    return round(min(stake, max_stake), 2)


def stake_to_win(combined_odds: int, target_win: float) -> float:
    """The stake so a win at `combined_odds` profits ~`target_win` (winnings, not payout)."""
    dec = _american_to_decimal(combined_odds)
    return round(target_win / (dec - 1.0), 2)


def profit_for_stake(combined_odds: int, stake: float) -> float:
    """Profit (winnings, excludes the stake) if a parlay at these odds wins."""
    return round(stake * (_american_to_decimal(combined_odds) - 1.0), 2)


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
