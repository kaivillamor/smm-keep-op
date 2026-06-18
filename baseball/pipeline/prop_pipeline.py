from datetime import datetime, timezone

from pipeline.stats_fetcher import (
    fetch_batter_recent_stats,
    fetch_batter_zone_stats,
    fetch_pitcher_zone_tendencies,
)
from model.factors.hr_prop_model import score_batter_hr_props, RECENT_DAYS

# Slightly under the final gate — filters out batters who clearly won't pass
# without spending API calls on recent/zone data for everyone.
_SEASON_PREFILTER = 55.0


def analyze_hr_props(
    lineups: dict,
    season_batter_stats: dict,
    probable_pitchers: dict,
) -> list[dict]:
    """
    Runs the 65/65/65 HR gate across every batter in confirmed lineups.

    lineups              : from fetch_lineups()
    season_batter_stats  : from fetch_batter_statcast_season()
    probable_pitchers    : from stats["probable_pitchers"]

    Returns a list of candidates who pass the gate, each with batter info + scores.
    Additional HR prop logic can be added here as new scoring functions in
    hr_prop_model.py — score_batter_hr_props() accepts any new metric naturally.
    """
    year = str(datetime.now(timezone.utc).year)
    candidates = []
    pitcher_zone_cache: dict[int, dict] = {}

    confirmed_games = sum(1 for v in lineups.values() if v.get("confirmed"))
    total_batters_checked = 0
    prefilter_passed = 0
    print(f"[prop_pipeline] {confirmed_games}/{len(lineups)} games have confirmed lineups")

    for game_pk, lineup_entry in lineups.items():
        if not lineup_entry.get("confirmed"):
            continue

        probable = _match_probable(game_pk, probable_pitchers)

        batter_groups = [
            (lineup_entry.get("home_lineup", []), probable.get("away_pitcher_id"), "home"),
            (lineup_entry.get("away_lineup", []), probable.get("home_pitcher_id"), "away"),
        ]

        for batters, pitcher_id, side in batter_groups:
            if not pitcher_id or not batters:
                continue

            if pitcher_id not in pitcher_zone_cache:
                pitcher_zone_cache[pitcher_id] = fetch_pitcher_zone_tendencies(pitcher_id, year)
            pitcher_zones = pitcher_zone_cache[pitcher_id]

            for batter in batters:
                batter_id = batter.get("id")
                if not batter_id:
                    continue

                total_batters_checked += 1
                season_stats = season_batter_stats.get(str(batter_id), {})
                if not _passes_season_prefilter(season_stats):
                    continue

                prefilter_passed += 1

                recent_stats = fetch_batter_recent_stats(batter_id, days=RECENT_DAYS)
                batter_zones = fetch_batter_zone_stats(batter_id, year)

                scores = score_batter_hr_props(
                    season_stats, recent_stats, batter_zones, pitcher_zones
                )

                if scores["passes_gate"]:
                    team_key = "home_team" if side == "home" else "away_team"
                    candidates.append({
                        "batter_id": batter_id,
                        "batter_name": batter.get("name"),
                        "team": lineup_entry.get(team_key),
                        "game_pk": game_pk,
                        "pitcher_id": pitcher_id,
                        "scores": scores,
                    })

    print(f"[prop_pipeline] Batters checked: {total_batters_checked} | "
          f"Passed pre-filter: {prefilter_passed} | "
          f"Passed gate: {len(candidates)}")
    return candidates


def _match_probable(game_pk, probable_pitchers: dict) -> dict:
    """Handles both int and str game_pk keys from the MLB Stats API."""
    return (
        probable_pitchers.get(int(game_pk))
        or probable_pitchers.get(str(game_pk))
        or {}
    )


def _passes_season_prefilter(season_stats: dict) -> bool:
    """
    Quick check before spending API calls on recent/zone data.
    Mirrors the OR gate — passes if either season stat clears the pre-filter floor.
    Zone Fit can't be pre-filtered (it's a per-matchup calculation), so batters with
    weak season stats but potentially strong zone fit will be caught at the gate stage.
    """
    sweet_spot = season_stats.get("sweet_spot_percent")
    hard_hit = season_stats.get("hard_hit_percent")
    if sweet_spot is None and hard_hit is None:
        return False
    return (sweet_spot is not None and sweet_spot >= _SEASON_PREFILTER) or \
           (hard_hit is not None and hard_hit >= _SEASON_PREFILTER)
