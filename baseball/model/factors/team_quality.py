WIN_PCT_W  = 0.25   # season win% differential
WRCP_W     = 0.40   # team wRC+ differential (park/league adjusted offense)
RUN_DIFF_W = 0.35   # run differential per game (captures both offense + pitching/defense)
_MAX_ADJ   = 0.15   # cap at ±15% — prevents early-season noise from dominating

_WRCP_NORM     = 20.0  # 20 wRC+ points = 1.0 unit signal (typical spread between good/bad teams)
_RUN_DIFF_NORM = 1.5   # 1.5 runs/game differential = 1.0 unit signal


def get_quality_adj(
    home_win_pct: float | None,  away_win_pct: float | None,
    home_wrc:     float | None,  away_wrc:     float | None,
    home_rdpg:    float | None,  away_rdpg:    float | None,
) -> float:
    """
    Adjusts home win probability based on three independent team quality signals.
    Each signal contributes independently — missing signals default to 0 contribution.
    Positive return = home team is meaningfully better quality than away team.

    Example — Dodgers (.623, wRC+ 115, +1.3 runs/game) visiting Twins (.481, wRC+ 94, -0.2):
      win_pct : (.481 - .623) × 0.40 = -0.057
      wRC+    : ((94 - 115) / 20) × 0.35 = -0.368
      run_diff: ((-0.2 - 1.3) / 1.5) × 0.25 = -0.250
      total   = -0.675 → capped at -0.15  (-15% off Twins win probability)
    """
    adj = 0.0

    if home_win_pct is not None and away_win_pct is not None:
        adj += (home_win_pct - away_win_pct) * WIN_PCT_W

    if home_wrc is not None and away_wrc is not None:
        adj += ((home_wrc - away_wrc) / _WRCP_NORM) * WRCP_W

    if home_rdpg is not None and away_rdpg is not None:
        adj += ((home_rdpg - away_rdpg) / _RUN_DIFF_NORM) * RUN_DIFF_W

    return round(max(-_MAX_ADJ, min(_MAX_ADJ, adj)), 4)
