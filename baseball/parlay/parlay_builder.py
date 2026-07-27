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
    """
    "Double-up, else $25 floor" staking, capped at max_stake.

      • Plus-money parlay (odds ≥ +100): a base-unit stake already wins ≥ its own
        size, so you double up (2x) or better — stake the base unit.
      • Minus-money parlay (odds < +100, a "safe" favorite): you can't double at
        the base, so stake enough to still clear the profit floor (default $25).
      • Never stake more than max_stake ($50). A parlay so heavily favored that the
        floor would need >$50 gets capped (profit then dips below the floor) — for
        a 2-leg parlay this only happens with two ~−450-or-heavier legs.

    At exactly +100 both branches agree (stake = base = floor by default).
    """
    m = _american_to_decimal(combined_odds) - 1.0   # profit per $1 staked
    stake = base_stake if m >= 1.0 else profit_floor / m
    return round(min(stake, max_stake), 2)


def stake_to_win(combined_odds: int, target_win: float) -> float:
    """
    Fixed-profit ("to-win") staking: the stake needed so a winning parlay at
    `combined_odds` profits ~`target_win` (winnings, not total payout).
        stake = target_win / (decimal_odds - 1)
    On longshots this shrinks the stake (limits bleed on low-hit-rate parlays);
    on near-even parlays it grows toward the target.
    """
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
