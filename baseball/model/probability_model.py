from model.factors.pitcher_ratings import score_pitchers
from model.factors.lineup_ratings import score_lineups
from model.factors.park_factors import get_runs_factor
from model.factors.weather_adjustments import get_weather_adjustment
from model.factors.owner_logic import apply_owner_logic

LEAGUE_AVG_TOTAL = 8.8        # total runs per game, both teams combined
HOME_FIELD_ADV   = 0.04       # ~54% home win rate historically
LEAGUE_AVG_FIP   = 4.15
RUNS_PER_FIP     = 0.45       # expected run change per 1.0 FIP unit per pitcher


def build_probabilities(odds: list, stats: dict, lineups: dict, weather: dict) -> list[dict]:
    results = []
    for game in odds:
        quant = _quant_score(game, stats, lineups, weather)
        owner_adj = apply_owner_logic(game, quant["win_pct"])

        final_win_pct = _clamp(quant["win_pct"] + owner_adj, 0.10, 0.90)

        results.append({
            "game_id":           game.get("id"),
            "home_team":         game.get("home_team"),
            "away_team":         game.get("away_team"),
            "our_home_win_pct":  final_win_pct,
            "our_implied_total": quant["implied_total"],
            "layer_scores": {
                "quant_win_pct":   quant["win_pct"],
                "owner_logic_adj": owner_adj,
                "llm_context_adj": 0.0,   # filled in by llm/context_analyzer.py
            },
            "factors": quant["factors"],
            "raw_odds": game,
        })

    return results


def _quant_score(game: dict, stats: dict, lineups: dict, weather: dict) -> dict:
    home_team  = game.get("home_team", "")
    probable   = stats.get("probable_pitchers", {})
    pitcher_stats = stats.get("pitcher_stats", {})

    # --- Win probability ---
    pitcher_adj = score_pitchers(game, stats)
    lineup_adj  = score_lineups(game, lineups, pitcher_stats, probable)
    win_pct     = _clamp(0.50 + HOME_FIELD_ADV + pitcher_adj + lineup_adj, 0.10, 0.90)

    # --- Expected run total ---
    park_runs     = get_runs_factor(home_team)
    weather_data  = weather.get(game.get("id"), {})
    weather_adj   = get_weather_adjustment(weather_data, home_team)

    game_entry    = _match_game(game, probable)
    if not game_entry:
        print(f"[probability_model] WARNING: no pitcher match for {game.get('away_team')} @ {game.get('home_team')} — using league avg total")
    home_pid      = str((game_entry or {}).get("home_pitcher_id") or "")
    away_pid      = str((game_entry or {}).get("away_pitcher_id") or "")

    # Each pitcher adjusts the opposing team's run expectation
    home_pitcher_runs_adj = _pitcher_run_delta(pitcher_stats.get(home_pid, {}))
    away_pitcher_runs_adj = _pitcher_run_delta(pitcher_stats.get(away_pid, {}))

    half_base       = (LEAGUE_AVG_TOTAL / 2) * park_runs
    away_team_runs  = half_base + home_pitcher_runs_adj   # home SP affects away scoring
    home_team_runs  = half_base + away_pitcher_runs_adj   # away SP affects home scoring
    implied_total   = round(max(away_team_runs + home_team_runs + weather_adj, 4.0), 2)

    return {
        "win_pct": win_pct,
        "implied_total": implied_total,
        "factors": {
            "home_field_adv":         HOME_FIELD_ADV,
            "pitcher_win_adj":        pitcher_adj,
            "lineup_win_adj":         lineup_adj,
            "park_runs_factor":       park_runs,
            "weather_run_adj":        weather_adj,
            "home_pitcher_run_delta": home_pitcher_runs_adj,
            "away_pitcher_run_delta": away_pitcher_runs_adj,
        },
    }


def _pitcher_run_delta(stats: dict) -> float:
    """
    How many more/fewer runs the opposing team scores vs a league-avg pitcher.
    Negative = pitcher is better than avg (suppresses runs).
    """
    if not stats:
        return 0.0
    fip  = stats.get("fip")  or LEAGUE_AVG_FIP
    xfip = stats.get("xfip") or fip
    blended = fip * 0.35 + xfip * 0.65
    return (blended - LEAGUE_AVG_FIP) * RUNS_PER_FIP


def _match_game(game: dict, probable: dict) -> dict | None:
    home = game.get("home_team", "").lower()
    away = game.get("away_team", "").lower()
    for entry in probable.values():
        if (entry.get("home_team", "").lower() == home and
                entry.get("away_team", "").lower() == away):
            return entry
    return None


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))
