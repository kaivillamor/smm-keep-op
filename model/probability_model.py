from model.factors.pitcher_ratings import score_pitchers
from model.factors.lineup_ratings import score_lineups
from model.factors.park_factors import get_park_factor
from model.factors.weather_adjustments import get_weather_adjustment
from model.factors.owner_logic import apply_owner_logic


def build_probabilities(odds: list, stats: dict, lineups: dict, weather: dict) -> list[dict]:
    results = []
    for game in odds:
        quant_score = _quant_score(game, stats, lineups, weather)
        owner_adjustment = apply_owner_logic(game, quant_score)
        # LLM adjustment (Layer 3) applied later in llm/context_analyzer.py

        results.append({
            "game_id": game.get("id"),
            "home_team": game.get("home_team"),
            "away_team": game.get("away_team"),
            "our_home_win_pct": quant_score + owner_adjustment,
            "layer_scores": {
                "quant": quant_score,
                "owner_logic": owner_adjustment,
                "llm_context": 0.0,
            },
            "raw_odds": game,
        })
    return results


def _quant_score(game: dict, stats: dict, lineups: dict, weather: dict) -> float:
    pass
