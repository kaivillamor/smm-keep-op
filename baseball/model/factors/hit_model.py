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

# Absurdity floor, NOT the playability test — that is the per-leg EV gate further down.
# Renamed from MIN_HIT_PROB on 2026-10-12 because the old name described a job it no
# longer does, and a constant whose name lies about its role is what hid a dead gate for
# three weeks (§29). Deliberately low and rarely binding: it only stops a leg the model
# rates worse than a coin flip from entering a parlay. Profitability is decided by edge
# against the leg's own price, never by a probability threshold.
MIN_LEG_PROB      = 0.50

# ── Betting-gate calibration (added 2026-09-09, refit 2026-09-30) ─────────────
# The model overstates its own confidence: regressed against reality, only a fraction of
# each predicted point survives. So the per-leg EV gate below decides on a SHRUNK
# probability, calibrated_hit_prob(), against each leg's own price.
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
# less data. Fitted on what the model ACTUALLY emitted, pathological rows included. Every
# one of those rows predates 2026-09-30, when _pitcher_hits_per_9 stopped flooring H/9 at
# 0.5 and began shrinking it by innings; excluding them gives 0.2893 ± 0.0908. A refit
# on rows scored the current way is PROJECT.md §27 #4.
#
# This first fed a constant threshold, MIN_HIT_PROB (0.73, then 0.654 from the median
# verified DraftKings price). A threshold set from the median cannot protect against
# selection — legs clearing 0.654 were priced at a mean 72.1% implied — so the per-leg
# EV gate replaced it on 2026-10-12. The derivation is in PROJECT.md §29.
CALIBRATION_SLOPE = 0.257   # how much of a predicted point survives contact with reality
CALIBRATION_BASE  = 0.6455  # observed base rate the shrink pulls toward


def calibrated_hit_prob(raw_prob: float) -> float:
    """Shrink a raw model probability toward the observed base rate.

    Used for the betting gate and EV. `model_prob` keeps the raw value; this one is
    logged beside it as `calibrated_prob`.
    """
    return round(CALIBRATION_BASE + CALIBRATION_SLOPE * (raw_prob - CALIBRATION_BASE), 4)


# ── Per-leg EV gate (2026-10-12) ──────────────────────────────────────────────
# Replaces the constant threshold, which was structurally wrong: a single number derived
# from the MEDIAN price cannot protect against selection, because the legs the model likes
# are not priced at the median. Measured — legs clearing the old 0.654 gate were priced at
# a mean 72.1% implied, not 65.4%. Live postseason confirmed it: 55 gate-clearing legs went
# 59.3% against a 68.4% book, a -9.2pt edge and -13.5% ROI, WORSE than the legs the gate
# rejected (-3.3pt). A gate that selects worse than no gate is anti-selective.
#
# The structure here is right: compare the model's probability to THAT leg's own price, so
# like is compared with like.
#
# MIN_EDGE_PTS derivation — from calibration uncertainty, not from a backtest optimum.
# CALIBRATION_SLOPE is 0.257 with a 95% CI of [0.099, 0.415]. Pushing those bounds through
# calibrated_hit_prob() moves the output by up to 3.2 pts over the usable raw range
# (largest at the top end, raw 0.85). An "edge" smaller than that is inside the noise of
# our own calibration, so the minimum is set above it. This is the opposite of how 0.73 was
# chosen: the number has a derivation that can be rechecked when the slope is refit.
MIN_EDGE_PTS = 0.05

# The regime the calibration constants were fit on. calibrated_hit_prob() carries a FIXED
# CALIBRATION_BASE, so it is only valid where that base rate holds. Out of regime it is
# biased, and the bias flows straight into the edge: the 2026 postseason base rate was
# 57.6% against the regular season's 64.5%, and every postseason calibrated_prob ran ~6 pts
# high as a result. Selecting on edge then selects for the BIAS rather than for edge, which
# is why tightening the threshold made out-of-sample results worse, not better.
CALIBRATION_PHASE = "regular"

# ── THE GATE IS NOT LIVE. Do not flip this without an out-of-sample test. ─────
# In-sample the per-leg gate looks excellent and the appearance is an artifact:
#
#     MIN_EDGE   in-sample (fit on these rows)   out-of-sample (postseason)
#      2.0 pt     +1.7 pts edge,  +2.9% ROI       -6.5 pts,  -11.5% ROI
#      5.0 pt     +7.4 pts edge, +12.8% ROI      -14.3 pts,  -26.1% ROI
#      8.0 pt    +15.9 pts edge, +28.8% ROI      -11.1 pts,  -21.9% ROI
#
# In-sample, tightening improves monotonically; out-of-sample it inverts. That is the
# signature of selecting on a fitted residual — the calibration was fit on the same rows,
# so `calibrated_prob - book_implied` partly encodes its own error, and a tighter cut
# selects harder for that error. The out-of-sample column is confounded by the regime shift
# above, so it is not proof the gate is bad; what it does prove is that the in-sample
# column is not evidence the gate is good. Neither number licenses risking money.
#
# To validate: NBA week one, on a VERIFIED price feed (§27 #2), fitting the calibration on
# one span and testing selection on a later one. Flip this to True only when an
# out-of-sample span shows a positive edge with an interval that excludes zero.
EV_GATE_LIVE = False


def leg_edge(calibrated_prob: float | None, book_implied: float | None) -> float | None:
    """Model edge on one leg, in probability points. None when the leg has no price.

    None rather than 0.0: an unpriced leg has an UNKNOWN edge, and a zero would quietly
    read as "no edge" and sort alongside genuinely fair-priced legs.
    """
    if calibrated_prob is None or book_implied is None:
        return None
    return round(calibrated_prob - book_implied, 4)


def leg_gate_reason(calibrated_prob: float | None,
                    book_implied: float | None,
                    phase: str | None) -> tuple[str, str] | None:
    """None if the leg may be surfaced, else (code, human_detail) saying why not.

    Returns a REASON rather than a bare False on purpose. "Nothing qualified today" was
    accepted as an honest result for three weeks while the gate was arithmetically dead
    (§29); silence is a value, and it has to be able to explain itself. The code is stable
    for counting, the detail carries the numbers.
    """
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
# defensible alone, were jointly impossible. The guard now checks MIN_LEG_PROB, the floor
# that replaced MIN_HIT_PROB on 2026-10-12, so the same trap cannot reopen there.
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
      4. (venue layer removed 2026-09-08 — see §4 below)
      5. Team recent offensive form — TEAM_RECENT_WEIGHT
      6. Day game penalty — flat −DAY_GAME_BA_PENALTY

    Pitcher quality factor:
      Season H/9, shrunk toward league average by innings, blended with last-3-starts
      H/9 at PITCHER_RECENT_WEIGHT. No days-rest adjustment (removed — see below).

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

    # ── 4. Venue blend REMOVED (2026-09-08) ───────────────────────────────────
    # fetch_batter_venue_stats never returned park-specific data: the MLB Stats API
    # silently ignores the `venueId` parameter on career stats, so it handed back the
    # batter's full CAREER line for every ballpark. With MIN_VENUE_AB=20 against career
    # AB in the thousands the gate always passed, so this layer was applying a hidden 5%
    # shrink toward each batter's career average while contributing zero park signal.
    # Not repaired, because park effects are already handled correctly and independently
    # by park_factors.get_park_factor (Coors 1.37, SF 0.93) further down — a working
    # venue blend would only double-count it.

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
