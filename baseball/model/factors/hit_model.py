LEAGUE_AVG_BA  = 0.248   # 2024 MLB batting average
LEAGUE_H_PER_9 = 8.5     # hits allowed per 9 innings, league-average pitcher
MIN_SPLIT_AB   = 30      # minimum AB before trusting a L/R split
MIN_H2H_AB     = 10      # minimum career AB vs this pitcher before applying H2H blend
MIN_RECENT_AB  = 10      # minimum AB in recent window before applying form adjustment

# ── Signal weights ────────────────────────────────────────────────────────────
# Batter BA blending (applied in order — each layer adjusts from the previous):
H2H_WEIGHT         = 0.40   # MAX weight on career avg vs this pitcher (see H2H_SHRINK_AB)
# Batter-vs-pitcher samples are tiny and enormously noisy — 4-for-10 reads as ".400".
# Giving that the full 40% was the single largest source of over-prediction, producing
# implied batting averages north of .500. Weight now scales with sample size,
#   weight = H2H_WEIGHT * ab / (ab + H2H_SHRINK_AB)
# so 10 AB earns ~9% of the max, 50 AB ~33%, 200 AB ~67%. It never reaches 40%.
H2H_SHRINK_AB      = 100    # AB at which H2H earns half its maximum weight
RECENT_WEIGHT      = 0.20   # last-14d hot/cold ratio multiplier on blended BA
TEAM_RECENT_WEIGHT = 0.10   # team-level hot/cold multiplier (collective lineup rhythm)

# Pitcher quality blending:
PITCHER_RECENT_WEIGHT = 0.40  # last-3-starts H/9 blended with season H/9
# (days-rest adjustment removed — see the note in score_batter_hit_prob)

# Situational:
DAY_GAME_BA_PENALTY = 0.010  # flat BA reduction — batters hit slightly worse in day games

# Hard ceiling on the fully-adjusted single-PA hit probability. Six upward nudges
# (H2H, recent form, venue, team form, pitcher quality, park) multiply together with
# no regression to the mean, so the old .600 cap let predictions imply batting averages
# of .500+. The modern single-season record is .426 and no one has hit .400 since 1941 —
# any game-level estimate above .400 is the stack compounding, not a real matchup.
MAX_ADJUSTED_BA     = 0.400

HIT_PARLAY_LEGS   = 4     # legs surfaced in the daily hit parlay (2 pairs × 2 legs)
MIN_HIT_PROB      = 0.654 # implied prob of the verified DK median — derived below

# ── Betting-gate calibration (added 2026-09-09, refit 2026-09-30) ─────────────
# The model overstates its own confidence: regressed against reality, only a fraction of
# each predicted point survives. So the gate compares a SHRUNK probability against
# MIN_HIT_PROB, via calibrated_hit_prob().
#
# The raw number is still what gets logged as predictions.model_prob — compressing that
# would split the calibration series into two model versions and make the sample
# unusable, which is already the problem with the pre-fix 986. The shrunk value is now
# logged ALONGSIDE it as predictions.calibrated_prob (2026-09-30). It is the number the
# gate actually decides on, and discarding it hid the bug in the guard below for three
# weeks: every analysis of "what the gate selects" silently measured model_prob instead.
#
# Refit 2026-09-30 on 2,598 deduplicated graded rows (the original used 240):
#   slope 0.2570 ± 0.0804, 95% CI [0.099, 0.415]   base = observed rate 0.6455
# The old 0.17 sits inside that interval, so it was never wrong — only fitted on 10x
# less data. Fitted on what the model ACTUALLY emits, pathological rows included: while
# _pitcher_hits_per_9 still floors at 0.5 the live model keeps producing them, so the
# calibration has to map its real output. Excluding them gives 0.2893 ± 0.0908.
#
# MIN_HIT_PROB derivation (2026-09-30). For a parlay of equally-priced legs the per-leg
# break-even equals the single-leg implied probability, so the gate is the implied prob
# of the median price actually available. Verified DraftKings, 915 graded priced rows:
#   p25 -219 (68.7%) | median -189 (65.4%) | p75 -163 (62.0%)
# -> 0.654. This REPLACES the old 0.73, which came from a -270 median the broken FanDuel
# feed invented (PROJECT.md §28). Known limitation: a constant derived from the median
# does not protect against selection — on the 2,598-row corpus the legs that clear this
# gate are priced at a mean 72.1% implied, not 65.4%, for a -4.1pt edge. The per-leg EV
# gate (compare calibrated_prob to each leg's own book_implied) is the agreed fallback.
CALIBRATION_SLOPE = 0.257   # how much of a predicted point survives contact with reality
CALIBRATION_BASE  = 0.6455  # observed base rate the shrink pulls toward


def calibrated_hit_prob(raw_prob: float) -> float:
    """Shrink a raw model probability toward the observed base rate.

    Used for the betting gate and EV. `model_prob` keeps the raw value; this one is
    logged beside it as `calibrated_prob`.
    """
    return round(CALIBRATION_BASE + CALIBRATION_SLOPE * (raw_prob - CALIBRATION_BASE), 4)


MAX_LINEUP_DEPTH  = 6     # only score batters in positions 1–6 (most plate appearances)

# "Double-up, else $25 floor" staking (see parlay_builder.recommended_stake):
#   plus-money parlay → stake the base unit (win 2x+);
#   minus-money "safe" parlay → stake enough to clear the profit floor.
HIT_BASE_STAKE    = 25.0  # base unit staked on a plus-money (double-up) parlay
HIT_PROFIT_FLOOR  = 25.0  # minimum profit to guarantee on a minus-money parlay
HIT_MAX_STAKE     = 50.0  # never stake more than this, even to hit the floor

# Batting average is hits per AT-BAT, but the lineup table below is PLATE APPEARANCES.
# Roughly 11% of PA (walks, HBP, sacrifices) can never produce a hit, so exponentiating
# by PA credited every batter ~0.5 extra chances and inflated every prediction by ~4 pts.
# 2026 league ratio, measured from the MLB Stats API team totals (107,826 AB / 121,394 PA).
AB_PER_PA = 0.888

# Historical expected PAs per game by batting order position.
_PA_BY_POS = {
    1: 4.6, 2: 4.4, 3: 4.3, 4: 4.1,
    5: 4.0, 6: 3.9, 7: 3.8, 8: 3.7, 9: 3.5,
}


# ── Gate reachability guard (added 2026-09-30) ────────────────────────────────
# From 2026-09-09 to 2026-09-30 the gate could not fire at ANY input. Shrinking toward
# 0.61 with slope 0.17 capped calibrated_hit_prob() at 0.676 while MIN_HIT_PROB was
# 0.73: clearing it needed a raw probability of 1.316. Zero of 2,598 graded rows cleared
# it, and from outside that looked like a finding ("the market is priced past the model")
# rather than a bug, because the compared value was never logged. Two constants, each
# defensible alone, were jointly impossible.
#
# The bound that matters is the model's STRUCTURAL ceiling, not calibrated_hit_prob(1.0):
# raw output is capped by MAX_ADJUSTED_BA over the most plate appearances any scored
# lineup slot gets, which is 0.8759 — so a gate can pass a raw=1.0 check and still be
# unreachable in practice. Checking the real ceiling is what makes this guard bite.
#
# A raise rather than an assert: `python -O` strips asserts, and this must hold in
# production, where a dead gate is invisible.
_MAX_PA  = max(pa for pos, pa in _PA_BY_POS.items() if pos <= MAX_LINEUP_DEPTH)
_MAX_RAW = 1.0 - (1.0 - MAX_ADJUSTED_BA) ** (_MAX_PA * AB_PER_PA)
_CEILING = calibrated_hit_prob(_MAX_RAW)
if _CEILING < MIN_HIT_PROB:
    raise RuntimeError(
        f"hit gate is unreachable: the model's highest possible raw probability "
        f"({_MAX_RAW:.4f}) calibrates to {_CEILING:.4f}, below MIN_HIT_PROB "
        f"({MIN_HIT_PROB}). No lineup can ever clear the gate. Move MIN_HIT_PROB, "
        f"CALIBRATION_SLOPE ({CALIBRATION_SLOPE}) or CALIBRATION_BASE "
        f"({CALIBRATION_BASE}) — do not ship a gate that cannot fire."
    )


def score_batter_hit_prob(
    batter_splits: dict,
    pitcher_stats: dict,
    pitcher_hand: str,
    lineup_pos: int,
    park_factor: float = 1.0,
    h2h_stats: dict | None = None,
    recent_ba_stats: dict | None = None,
    venue_stats: dict | None = None,   # accepted but IGNORED — see §4 below
    pitcher_recent: dict | None = None,
    team_recent: dict | None = None,
    is_day_game: bool = False,
) -> float:
    """
    Returns P(batter gets 1+ hits) for one game.

    Batter BA layers (applied in order):
      1. Season BA vs pitcher handedness (L/R split)
      2. H2H career avg vs this specific pitcher   — 40% blend
      3. Recent form (L14d BA) hot/cold multiplier — 20%
      4. Venue career avg                          —  5%
      5. Team recent offensive form                — 10%
      6. Day game penalty                          — flat –0.010

    Pitcher quality factor:
      Season H/9 blended 60/40 with last-3-starts H/9.
      Days-rest deviation from 4-day norm adjusts quality factor ±1%/day.
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

    # ── 4. Venue blend REMOVED (2026-09-08) ───────────────────────────────────
    # fetch_batter_venue_stats never returned park-specific data: the MLB Stats API
    # silently ignores the `venueId` parameter on career stats, so it handed back the
    # batter's full CAREER line for every ballpark. With MIN_VENUE_AB=20 against career
    # AB in the thousands the gate always passed, so this layer was applying a hidden 5%
    # shrink toward each batter's career average while contributing zero park signal.
    # Not repaired, because park effects are already handled correctly and independently
    # by park_factors.get_park_factor (Coors 1.37, SF 0.93) further down — a working
    # venue blend would only double-count it. `venue_stats` is still accepted so the
    # frozen versions/8leg_10 caller keeps working, but it is ignored.

    # ── 5. Team recent offense multiplier ────────────────────────────────────
    if team_recent and team_recent.get("avg") is not None:
        team_ratio = team_recent["avg"] / LEAGUE_AVG_BA
        batter_ba *= 1 + (team_ratio - 1) * TEAM_RECENT_WEIGHT

    # ── 6. Day game penalty ───────────────────────────────────────────────────
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

    # Days-rest adjustment REMOVED. It applied ±1%/day with the sign inverted (short
    # rest predicted FEWER hits allowed, which is backwards for a fatigued pitcher),
    # and the true effect is pitcher-specific rather than league-wide — some benefit
    # from extra rest, others go rusty. Rather than apply a blanket adjustment whose
    # direction we can't justify, apply none. `days_rest` is still fetched and stored
    # on every research prediction, so the data can settle the direction empirically.

    # ── Park + final calculation ──────────────────────────────────────────────
    park_adj    = 1.0 + (park_factor - 1.0) * 0.3
    expected_pa = _PA_BY_POS.get(lineup_pos, 4.0)
    expected_ab = expected_pa * AB_PER_PA   # BA is per at-bat, not per plate appearance
    adjusted_ba = min(batter_ba * quality_factor * park_adj, MAX_ADJUSTED_BA)

    return round(1.0 - (1.0 - adjusted_ba) ** expected_ab, 4)


# Innings of league-average prior mixed into every pitcher's H/9. A September call-up
# with 2 innings gets pulled ~94% of the way to league average; a 180-inning starter keeps
# ~86% of his own rate. Chosen so a single short outing cannot masquerade as dominance.
H9_IP_PRIOR = 30.0


def _pitcher_hits_per_9(pitcher_stats: dict) -> float:
    """H/9 = WHIP×9 − BB/9, regressed toward league average by innings pitched.

    The regression is the point. This used to end in `max(..., 0.5)`, and that floor —
    not a small sample — is where Jared Simpson's 0.5 H/9 came from: it drove six
    Phillies batters to 4-6% hit probability on 2026-09-16, including Harper at 6.1% and
    Arraez at 6.4%, both of whom got hits. Across the 138 rows with a sub-4.0 H/9 the
    model predicted 45.4% where reality delivered 61.6% (§29).

    Note the asymmetry that bug had: when stats were entirely MISSING the function
    returned LEAGUE_H_PER_9, which is right, but when they were present and tiny it
    returned 0.5. Sparse data was handled worse than absent data. Now a rate with no
    innings behind it is treated as no rate at all, and a thin sample is shrunk rather
    than trusted.
    """
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
        # A rate with no innings behind it is not a rate. Previously this branch could
        # still return a raw number computed from a 2-inning sample.
        return LEAGUE_H_PER_9

    weight = ip / (ip + H9_IP_PRIOR)
    return max(weight * raw + (1.0 - weight) * LEAGUE_H_PER_9, 0.0)
