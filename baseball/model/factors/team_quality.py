WIN_PCT_W  = 0.25   # season win% differential
OFFENSE_W  = 0.40   # team wRC+ (park/league adjusted offensive output)
RUN_DIFF_W = 0.35   # run differential per game (captures both offense + pitching/defense)
_MAX_ADJ   = 0.15   # cap at ±15% — prevents early-season noise from dominating

_OFFENSE_NORM  = 20.0  # 20 wRC+ points = 1.0 unit signal (typical spread between good/bad teams)
_RUN_DIFF_NORM = 1.5   # 1.5 runs/game differential = 1.0 unit signal


def get_quality_adj(
    home_win_pct:  float | None,  away_win_pct:  float | None,
    home_offense:  float | None,  away_offense:  float | None,
    home_rdpg:     float | None,  away_rdpg:     float | None,
) -> float:
    """
    Adjusts home win probability based on three independent team quality signals.
    Each signal contributes independently — missing signals default to 0 contribution.
    Positive return = home team is meaningfully better quality than away team.

    home_offense / away_offense are wRC+ values computed from MLB Stats API batting stats.
    100 = league avg; 115 = 15% above avg. 20-point spread = 1 unit signal.

    Example — Dodgers (.623, wRC+ 118, +1.3 rdpg) hosting Twins (.481, wRC+ 95, -0.2):
      win_pct : (.623 - .481) × 0.25 = +0.036
      offense : ((118 - 95) / 20) × 0.40 = +0.460
      run_diff: ((1.3 - -0.2) / 1.5) × 0.35 = +0.350
      total   = +0.846 → capped at +0.15
    """
    adj = 0.0

    if home_win_pct is not None and away_win_pct is not None:
        adj += (home_win_pct - away_win_pct) * WIN_PCT_W

    if home_offense is not None and away_offense is not None:
        adj += ((home_offense - away_offense) / _OFFENSE_NORM) * OFFENSE_W

    if home_rdpg is not None and away_rdpg is not None:
        adj += ((home_rdpg - away_rdpg) / _RUN_DIFF_NORM) * RUN_DIFF_W

    return round(max(-_MAX_ADJ, min(_MAX_ADJ, adj)), 4)
