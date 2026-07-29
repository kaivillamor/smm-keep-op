from pipeline.stats_fetcher import (
    fetch_batter_splits,
    fetch_batter_vs_pitcher,
    fetch_batter_recent_ba,
    fetch_batter_venue_stats,
    get_venue_id,
    fetch_pitcher_recent_form,
    fetch_team_recent_hitting,
    get_team_id,
    game_has_started,
    is_day_game,
)
from model.factors.hit_model import (
    score_batter_hit_prob,
    HIT_PARLAY_LEGS,
    MAX_LINEUP_DEPTH,
    MIN_HIT_PROB,
)
from model.factors.park_factors import get_park_factor
from model.factors.owner_logic import apply_hit_owner_logic
from pipeline.prop_odds import fetch_hit_prop_odds, normalize_name


def analyze_hit_props(lineups: dict, stats: dict,
                      all_candidates_out: list | None = None) -> list[dict]:
    """
    Scores batters in positions 1–MAX_LINEUP_DEPTH of every confirmed lineup
    on P(1+ hit) for today's matchup. Returns the top HIT_PARLAY_LEGS candidates.

    lineups : from fetch_lineups()
    stats   : from fetch_stats() — needs probable_pitchers and pitcher_stats
    """
    probable      = stats.get("probable_pitchers", {})
    pitcher_stats = stats.get("pitcher_stats", {})

    confirmed_count = sum(1 for v in lineups.values() if v.get("confirmed"))
    # Games already underway can't be bet — drop them and carry on with the rest of
    # the slate rather than treating a started game as a reason to produce nothing.
    started = sum(1 for v in lineups.values()
                  if v.get("confirmed") and game_has_started(v.get("commence_time")))
    bettable = confirmed_count - started
    print(f"[hit_pipeline] {confirmed_count}/{len(lineups)} games have confirmed lineups"
          + (f" | {started} already started → {bettable} bettable" if started else ""))

    candidates: list[dict] = []
    pitcher_recent_cache: dict[int, dict] = {}
    team_recent_cache:    dict[str, dict] = {}

    for game_pk, lineup_entry in lineups.items():
        if not lineup_entry.get("confirmed"):
            continue
        if game_has_started(lineup_entry.get("commence_time")):
            continue

        probable_entry = _match_probable(game_pk, probable)
        if not probable_entry:
            continue

        home_team = lineup_entry.get("home_team", "")
        runs_factor, _ = get_park_factor(home_team)
        venue_id = get_venue_id(home_team)

        commence = lineup_entry.get("commence_time", "")
        day_game = is_day_game(commence)

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
                    is_day_game=day_game,
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
                    "is_day_game":       day_game,
                    "base_prob":       base_prob,
                    "owner_adj":       owner_adj,
                    "hit_probability": prob,
                    "game_pk":         game_pk,
                })

    candidates.sort(key=lambda c: c["hit_probability"], reverse=True)

    # Optional hand-off of EVERY scored batter for calibration research. The surfaced
    # legs alone are a biased sample (the ones the model already liked), which can't
    # reveal whether the ranking works. Default None keeps existing callers unchanged.
    if all_candidates_out is not None:
        all_candidates_out.extend(candidates)

    top = _select_legs(candidates, HIT_PARLAY_LEGS)

    # Attach real book odds (prop-odds API) and compute EV = our prob − book implied.
    # No legs are dropped for negative EV — we surface the same picks, ranked by edge.
    _attach_hit_odds(top)
    # Rank by EV descending; legs with no posted line (ev=None) sort to the bottom.
    # Use -inf rather than `or -1` so an EV of exactly 0.0 isn't mis-sorted as negative.
    top.sort(key=lambda c: c["ev"] if c.get("ev") is not None else float("-inf"),
             reverse=True)

    print(
        f"[hit_pipeline] {len(candidates)} batters scored | "
        f"{len(top)} surfaced as hit parlay legs"
    )
    return top


def _attach_hit_odds(legs: list[dict]) -> None:
    """Look up each leg's 1+ hit odds from the prop-odds API by player name and attach
    book_odds / book_implied / book / ev in place. Legs with no posted line keep
    ev=None and sort to the bottom."""
    if not legs:
        return
    board = fetch_hit_prop_odds()
    matched = 0
    for leg in legs:
        entry = board.get(normalize_name(leg.get("batter_name", "")))
        if not entry:
            leg["book_odds"] = leg["book_implied"] = leg["book"] = leg["ev"] = None
            continue
        matched += 1
        leg["book_odds"]    = entry["odds"]
        leg["book_implied"] = entry["implied"]
        leg["book"]         = entry["book"]
        # EV as a probability edge: how much higher our estimate is than the book's
        leg["ev"] = (round(leg["hit_probability"] - entry["implied"], 4)
                     if entry["implied"] is not None else None)
    print(f"[hit_pipeline] matched book odds for {matched}/{len(legs)} legs")


def _select_legs(candidates: list[dict], max_legs: int) -> list[dict]:
    """Pick the top max_legs candidates ≥ MIN_HIT_PROB with at most 1 leg per team."""
    seen_teams: set[str] = set()
    legs = []
    below_gate = 0
    for c in candidates:
        if c["hit_probability"] < MIN_HIT_PROB:
            below_gate += 1
            continue
        if c["team"] in seen_teams:
            continue
        seen_teams.add(c["team"])
        legs.append(c)
        if len(legs) >= max_legs:
            break
    if below_gate and len(legs) < max_legs:
        print(f"[hit_pipeline] Thin slate: only {len(legs)} leg(s) clear the "
              f"{MIN_HIT_PROB:.0%} probability gate — not padding with weaker legs.")
    return legs


def _match_probable(game_pk, probable: dict) -> dict | None:
    """Game IDs arrive as int from the schedule API and str from the saved lineup
    JSON, so try both. A non-numeric ID returns None rather than raising — a lookup
    miss should never take down the caller."""
    try:
        hit = probable.get(int(game_pk))
    except (TypeError, ValueError):
        hit = None
    return hit or probable.get(str(game_pk)) or None


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
