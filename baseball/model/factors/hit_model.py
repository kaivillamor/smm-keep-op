LEAGUE_AVG_BA  = 0.248   # 2024 MLB batting average
LEAGUE_H_PER_9 = 8.5     # hits allowed per 9 innings, league-average pitcher
MIN_SPLIT_AB   = 30      # minimum AB before trusting a L/R split
MIN_H2H_AB     = 10      # minimum career AB vs this pitcher before applying H2H blend
MIN_RECENT_AB  = 10      # minimum AB in recent window before applying form adjustment

# ── Signal weights ────────────────────────────────────────────────────────────
# Batter BA blending (applied in order — each layer adjusts from the previous):
H2H_WEIGHT         = 0.40   # MAX weight on career avg vs this pitcher (see H2H_SHRINK_AB)
H2H_SHRINK_AB      = 100    # AB at which H2H earns half its maximum weight
RECENT_WEIGHT      = 0.20   # last-14d hot/cold ratio multiplier on blended BA
TEAM_RECENT_WEIGHT = 0.10   # team-level hot/cold multiplier (collective lineup rhythm)

# Pitcher quality blending:
PITCHER_RECENT_WEIGHT = 0.40  # last-3-starts H/9 blended with season H/9

# Situational:
DAY_GAME_BA_PENALTY = 0.010  # flat BA reduction — batters hit slightly worse in day games

# Ceiling on the fully adjusted per-at-bat hit probability.
MAX_ADJUSTED_BA     = 0.400

HIT_PARLAY_LEGS   = 4     # legs surfaced in the daily hit parlay (2 pairs × 2 legs)

# Absurdity floor only: the per-leg EV gate decides playability.
MIN_LEG_PROB      = 0.50

# ── Betting-gate calibration ──────────────────────────────────────────────────
CALIBRATION_SLOPE = 0.257   # how much of a predicted point survives contact with reality
CALIBRATION_BASE  = 0.6455  # observed base rate the shrink pulls toward


def calibrated_hit_prob(raw_prob: float) -> float:
    """Shrink a raw probability toward the observed base rate: the value the gate and EV
    use, logged as `calibrated_prob` beside the raw `model_prob`."""
    return round(CALIBRATION_BASE + CALIBRATION_SLOPE * (raw_prob - CALIBRATION_BASE), 4)


# ── Per-leg EV gate ───────────────────────────────────────────────────────────
# Above the 3.2 pts that CALIBRATION_SLOPE's 95% CI [0.099, 0.415] can move a calibrated
# probability: recompute when the slope is refit.
MIN_EDGE_PTS = 0.05

# The regime the calibration was fit on; the gate refuses any other.
CALIBRATION_PHASE = "regular"

# Not live: flip only after an out-of-sample span shows edge with a CI above zero.
EV_GATE_LIVE = False


def leg_edge(calibrated_prob: float | None, book_implied: float | None) -> float | None:
    """Model edge on one leg in probability points; None (unknown, not 0) if unpriced."""
    if calibrated_prob is None or book_implied is None:
        return None
    return round(calibrated_prob - book_implied, 4)


def leg_gate_reason(calibrated_prob: float | None,
                    book_implied: float | None,
                    phase: str | None) -> tuple[str, str] | None:
    """None if the leg may be surfaced, else (code, human_detail) saying why not."""
    if not EV_GATE_LIVE:
        return ("not_validated",
                "EV gate is not validated out of sample (hit_model.EV_GATE_LIVE)")
    if calibrated_prob is None:
        return ("no_prob", "no calibrated probability")
    if calibrated_prob < MIN_LEG_PROB:
        return ("below_floor",
                f"calibrated {calibrated_prob:.1%} below the {MIN_LEG_PROB:.0%} floor")
    if book_implied is None:
        return ("unpriced", "unpriced — edge is not computable without a price")
    if phase != CALIBRATION_PHASE:
        return ("wrong_regime",
                f"phase {phase or 'unknown'} is outside the calibration regime "
                f"({CALIBRATION_PHASE})")
    edge = leg_edge(calibrated_prob, book_implied)
    if edge < MIN_EDGE_PTS:
        return ("below_edge",
                f"edge {edge * 100:+.1f}pt below the {MIN_EDGE_PTS * 100:.1f}pt minimum")
    return None


MAX_LINEUP_DEPTH  = 6     # only score batters in positions 1–6 (most plate appearances)

# Staking: parlay_builder.recommended_stake.
HIT_BASE_STAKE    = 25.0  # base unit staked on a plus-money (double-up) parlay
HIT_PROFIT_FLOOR  = 25.0  # minimum profit to guarantee on a minus-money parlay
HIT_MAX_STAKE     = 50.0  # never stake more than this, even to hit the floor

# BA is per at-bat but _PA_BY_POS is plate appearances: the 2026 league AB/PA.
AB_PER_PA = 0.888

# Historical expected PAs per game by batting order position.
_PA_BY_POS = {
    1: 4.6, 2: 4.4, 3: 4.3, 4: 4.1,
    5: 4.0, 6: 3.9, 7: 3.8, 8: 3.7, 9: 3.5,
}


# ── Gate reachability guard ───────────────────────────────────────────────────
# The model's structural ceiling (MAX_ADJUSTED_BA over the most PA a scored slot gets)
# must calibrate to at least MIN_LEG_PROB. A raise, not an assert: `-O` strips asserts.
_MAX_PA  = max(pa for pos, pa in _PA_BY_POS.items() if pos <= MAX_LINEUP_DEPTH)
_MAX_RAW = 1.0 - (1.0 - MAX_ADJUSTED_BA) ** (_MAX_PA * AB_PER_PA)
_CEILING = calibrated_hit_prob(_MAX_RAW)
if _CEILING < MIN_LEG_PROB:
    raise RuntimeError(
        f"leg floor is unreachable: the model's highest possible raw probability "
        f"({_MAX_RAW:.4f}) calibrates to {_CEILING:.4f}, below MIN_LEG_PROB "
        f"({MIN_LEG_PROB}). No lineup could ever clear it. Move MIN_LEG_PROB, "
        f"CALIBRATION_SLOPE ({CALIBRATION_SLOPE}) or CALIBRATION_BASE "
        f"({CALIBRATION_BASE}) — do not ship a threshold that cannot be met."
    )


def score_batter_hit_prob(
    batter_splits: dict,
    pitcher_stats: dict,
    pitcher_hand: str,
    lineup_pos: int,
    park_factor: float = 1.0,
    h2h_stats: dict | None = None,
    recent_ba_stats: dict | None = None,
    pitcher_recent: dict | None = None,
    team_recent: dict | None = None,
    is_day_game: bool = False,
) -> float:
    """
    Returns P(batter gets 1+ hits) for one game.

    Batter BA layers (applied in order):
      1. Season BA vs pitcher handedness (L/R split); league average under MIN_SPLIT_AB
      2. H2H career avg vs this pitcher — up to H2H_WEIGHT, scaled by sample size
      3. Recent form (L14d BA) hot/cold multiplier — RECENT_WEIGHT
      4. Team recent offensive form — TEAM_RECENT_WEIGHT
      5. Day game penalty — flat −DAY_GAME_BA_PENALTY

    Pitcher quality factor:
      Season H/9, shrunk toward league average by innings, blended with last-3-starts
      H/9 at PITCHER_RECENT_WEIGHT.

    Then park at 30% of its runs factor, a cap of MAX_ADJUSTED_BA per at-bat, and
    1 − (1 − BA)^AB over the lineup slot's expected at-bats.
    """
    # ── 1. Base BA from season L/R split ─────────────────────────────────────
    split_key = "vs_rhp" if pitcher_hand == "R" else "vs_lhp"
    split     = batter_splits.get(split_key, {})
    season_ba = split.get("avg")
    if not season_ba or season_ba <= 0 or split.get("ab", 0) < MIN_SPLIT_AB:
        season_ba = LEAGUE_AVG_BA
    batter_ba = season_ba

    # ── 2. H2H blend (weight scales with sample size) ────────────────────────
    if h2h_stats and h2h_stats.get("ab", 0) >= MIN_H2H_AB:
        h2h_ab    = h2h_stats.get("ab", 0)
        h2h_avg   = h2h_stats.get("avg") or 0.0
        h2h_w     = H2H_WEIGHT * h2h_ab / (h2h_ab + H2H_SHRINK_AB)
        batter_ba = batter_ba * (1 - h2h_w) + h2h_avg * h2h_w

    # ── 3. Recent form multiplier ─────────────────────────────────────────────
    if recent_ba_stats and recent_ba_stats.get("ab", 0) >= MIN_RECENT_AB:
        recent_avg = recent_ba_stats.get("avg")
        if recent_avg is not None:
            hot_cold   = recent_avg / (season_ba or LEAGUE_AVG_BA)
            batter_ba *= 1 + (hot_cold - 1) * RECENT_WEIGHT

    # ── 4. Team recent offense multiplier ────────────────────────────────────
    if team_recent and team_recent.get("avg") is not None:
        team_ratio = team_recent["avg"] / LEAGUE_AVG_BA
        batter_ba *= 1 + (team_ratio - 1) * TEAM_RECENT_WEIGHT

    # ── 5. Day game penalty ───────────────────────────────────────────────────
    if is_day_game:
        batter_ba -= DAY_GAME_BA_PENALTY

    batter_ba = max(batter_ba, 0.0)

    # ── Pitcher quality factor ────────────────────────────────────────────────
    season_h9 = _pitcher_hits_per_9(pitcher_stats)

    if pitcher_recent and pitcher_recent.get("h_per_9") is not None:
        pitcher_h9 = (season_h9 * (1 - PITCHER_RECENT_WEIGHT)
                      + pitcher_recent["h_per_9"] * PITCHER_RECENT_WEIGHT)
    else:
        pitcher_h9 = season_h9

    quality_factor = pitcher_h9 / LEAGUE_H_PER_9

    # ── Park + final calculation ──────────────────────────────────────────────
    park_adj    = 1.0 + (park_factor - 1.0) * 0.3
    expected_pa = _PA_BY_POS.get(lineup_pos, 4.0)
    expected_ab = expected_pa * AB_PER_PA   # BA is per at-bat, not per plate appearance
    adjusted_ba = min(batter_ba * quality_factor * park_adj, MAX_ADJUSTED_BA)

    return round(1.0 - (1.0 - adjusted_ba) ** expected_ab, 4)


# Innings of league-average prior mixed into every pitcher's H/9.
H9_IP_PRIOR = 30.0


def _pitcher_hits_per_9(pitcher_stats: dict) -> float:
    """H/9 = WHIP×9 − BB/9, regressed toward league average by innings pitched; league
    average when there is no rate or no innings."""
    whip    = pitcher_stats.get("whip")
    bb9     = pitcher_stats.get("bb_per_9")
    opp_avg = pitcher_stats.get("opp_avg")

    if whip is not None and bb9 is not None:
        raw = whip * 9 - bb9        # exact: WHIP×9 = H/9 + BB/9, so this is H/9
    elif whip is not None:
        raw = whip * 9 - 3.2        # league-average BB/9 stand-in
    elif opp_avg is not None:
        raw = opp_avg * 34          # opponent BA -> H/9 at ~34 AB per 9 innings
    else:
        return LEAGUE_H_PER_9

    ip = pitcher_stats.get("innings_pitched")
    if not ip or ip <= 0:
        return LEAGUE_H_PER_9

    weight = ip / (ip + H9_IP_PRIOR)
    return max(weight * raw + (1.0 - weight) * LEAGUE_H_PER_9, 0.0)
