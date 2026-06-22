from pipeline.stats_fetcher import (
    fetch_batter_splits,
    fetch_batter_vs_pitcher,
    fetch_batter_recent_ba,
    fetch_batter_venue_stats,
    get_venue_id,
    fetch_pitcher_recent_form,
    fetch_team_recent_hitting,
    get_team_id,
)
from model.factors.hit_model import (
    score_batter_hit_prob,
    HIT_PARLAY_LEGS,
    MAX_LINEUP_DEPTH,
)
from model.factors.park_factors import get_park_factor
from model.factors.owner_logic import apply_hit_owner_logic


def analyze_hit_props(lineups: dict, stats: dict) -> list[dict]:
    """
    Scores batters in positions 1–MAX_LINEUP_DEPTH of every confirmed lineup
    on P(1+ hit) for today's matchup. Returns the top HIT_PARLAY_LEGS candidates.

    lineups : from fetch_lineups()
    stats   : from fetch_stats() — needs probable_pitchers and pitcher_stats
    """
    probable      = stats.get("probable_pitchers", {})
    pitcher_stats = stats.get("pitcher_stats", {})

    confirmed_count = sum(1 for v in lineups.values() if v.get("confirmed"))
    print(f"[hit_pipeline] {confirmed_count}/{len(lineups)} games have confirmed lineups")

    candidates: list[dict] = []
    pitcher_recent_cache: dict[int, dict] = {}
    team_recent_cache:    dict[str, dict] = {}

    for game_pk, lineup_entry in lineups.items():
        if not lineup_entry.get("confirmed"):
            continue

        probable_entry = _match_probable(game_pk, probable)
        if not probable_entry:
            continue

        home_team = lineup_entry.get("home_team", "")
        runs_factor, _ = get_park_factor(home_team)
        venue_id = get_venue_id(home_team)

        commence = lineup_entry.get("commence_time", "")
        try:
            utc_hour   = int(commence[11:13])
            is_day_game = utc_hour < 22   # before 6pm ET (EDT = UTC-4)
        except (ValueError, IndexError):
            is_day_game = False

        away_team = lineup_entry.get("away_team", "")

        groups = [
            (
                lineup_entry.get("home_lineup", []),
                probable_entry.get("away_pitcher_id"),
                probable_entry.get("away_pitcher_name", "TBD"),
                probable_entry.get("away_pitcher_throws", "R"),
                home_team,
                away_team,    # opponent of the home batters
            ),
            (
                lineup_entry.get("away_lineup", []),
                probable_entry.get("home_pitcher_id"),
                probable_entry.get("home_pitcher_name", "TBD"),
                probable_entry.get("home_pitcher_throws", "R"),
                away_team,
                home_team,    # opponent of the away batters
            ),
        ]

        for batters, pitcher_id, pitcher_name, pitcher_hand, team, opponent_team in groups:
            if not pitcher_id or not batters:
                continue

            p_stats = pitcher_stats.get(str(pitcher_id), {})

            if pitcher_id not in pitcher_recent_cache:
                pitcher_recent_cache[pitcher_id] = fetch_pitcher_recent_form(pitcher_id)
            pitcher_recent = pitcher_recent_cache[pitcher_id]

            for idx, batter in enumerate(batters):
                batter_id = batter.get("id")
                if not batter_id:
                    continue

                lineup_pos = _normalize_batting_pos(batter.get("batting_order"), idx)
                if lineup_pos > MAX_LINEUP_DEPTH:
                    continue

                batter_name  = batter.get("name", "")

                if team not in team_recent_cache:
                    tid = get_team_id(team)
                    team_recent_cache[team] = fetch_team_recent_hitting(tid) if tid else {}
                team_recent = team_recent_cache[team]

                splits       = fetch_batter_splits(batter_id)
                h2h_stats    = fetch_batter_vs_pitcher(batter_id, pitcher_id)
                recent_stats = fetch_batter_recent_ba(batter_id)
                venue_stats  = fetch_batter_venue_stats(batter_id, venue_id) if venue_id else {}

                base_prob = score_batter_hit_prob(
                    splits, p_stats, pitcher_hand, lineup_pos, runs_factor,
                    h2h_stats=h2h_stats,
                    recent_ba_stats=recent_stats,
                    venue_stats=venue_stats,
                    pitcher_recent=pitcher_recent,
                    team_recent=team_recent,
                    is_day_game=is_day_game,
                )

                owner_adj = apply_hit_owner_logic(batter_name, opponent_team, base_prob)
                prob = round(min(max(base_prob + owner_adj, 0.0), 1.0), 4)

                candidates.append({
                    "batter_id":       batter_id,
                    "batter_name":     batter_name,
                    "team":            team,
                    "opponent_team":   opponent_team,
                    "lineup_pos":      lineup_pos,
                    "pitcher_name":    pitcher_name,
                    "pitcher_hand":    pitcher_hand,
                    "h2h_ab":          h2h_stats.get("ab", 0),
                    "h2h_avg":         h2h_stats.get("avg"),
                    "recent_ab":       recent_stats.get("ab", 0),
                    "recent_avg":      recent_stats.get("avg"),
                    "venue_ab":          venue_stats.get("ab", 0),
                    "venue_avg":         venue_stats.get("avg"),
                    "pitcher_recent_h9": pitcher_recent.get("h_per_9"),
                    "pitcher_days_rest": pitcher_recent.get("days_rest"),
                    "team_recent_avg":   team_recent.get("avg"),
                    "is_day_game":       is_day_game,
                    "base_prob":       base_prob,
                    "owner_adj":       owner_adj,
                    "hit_probability": prob,
                    "game_pk":         game_pk,
                })

    candidates.sort(key=lambda c: c["hit_probability"], reverse=True)
    top = _select_legs(candidates, HIT_PARLAY_LEGS)

    print(
        f"[hit_pipeline] {len(candidates)} batters scored | "
        f"{len(top)} surfaced as hit parlay legs"
    )
    return top


def _select_legs(candidates: list[dict], max_legs: int) -> list[dict]:
    """Pick the top max_legs candidates with at most 1 leg per team."""
    seen_teams: set[str] = set()
    legs = []
    for c in candidates:
        if c["team"] in seen_teams:
            continue
        seen_teams.add(c["team"])
        legs.append(c)
        if len(legs) >= max_legs:
            break
    return legs


def _match_probable(game_pk, probable: dict) -> dict | None:
    return (
        probable.get(int(game_pk))
        or probable.get(str(game_pk))
        or None
    )


def _normalize_batting_pos(batting_order, fallback_idx: int) -> int:
    """
    MLB Stats API boxscore returns battingOrder as 100, 200, … (position × 100).
    The /lineups endpoint may return 1, 2, … or None.
    Fall back to list index + 1 when the field is absent.
    """
    if batting_order is None:
        return fallback_idx + 1
    order = int(batting_order)
    if order >= 100:
        return order // 100
    return order
