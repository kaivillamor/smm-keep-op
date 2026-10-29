from pipeline.stats_fetcher import (
    fetch_batter_splits,
    fetch_batter_vs_pitcher,
    fetch_batter_recent_ba,
    fetch_pitcher_recent_form,
    fetch_team_recent_hitting,
    get_team_id,
    game_has_started,
    game_slate_date,
    is_day_game,
    season_phase,
    _match_probable,
)
from model.factors.hit_model import (
    score_batter_hit_prob,
    calibrated_hit_prob,
    _pitcher_hits_per_9,
    HIT_PARLAY_LEGS,
    MAX_LINEUP_DEPTH,
    MIN_EDGE_PTS,
    leg_edge,
    leg_gate_reason,
)
from model.factors.park_factors import get_park_factor
from model.factors.owner_logic import apply_hit_owner_logic
from pipeline.prop_odds import fetch_hit_prop_odds, normalize_name


def analyze_hit_props(lineups: dict, stats: dict,
                      all_candidates_out: list | None = None) -> list[dict]:
    """
    Scores batters in positions 1–MAX_LINEUP_DEPTH of every confirmed lineup
    on P(1+ hit) for today's matchup. Returns up to HIT_PARLAY_LEGS legs that clear
    the per-leg EV gate, ranked by edge (see _select_legs).

    lineups : from fetch_lineups()
    stats   : from fetch_stats() — needs probable_pitchers and pitcher_stats
    """
    probable      = stats.get("probable_pitchers", {})
    pitcher_stats = stats.get("pitcher_stats", {})

    confirmed_count = sum(1 for v in lineups.values() if v.get("confirmed"))
    # Games already underway can't be bet: drop them and score the rest.
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

                base_prob = score_batter_hit_prob(
                    splits, p_stats, pitcher_hand, lineup_pos, runs_factor,
                    h2h_stats=h2h_stats,
                    recent_ba_stats=recent_stats,
                    pitcher_recent=pitcher_recent,
                    team_recent=team_recent,
                    is_day_game=day_game,
                )

                owner_adj = apply_hit_owner_logic(batter_name, opponent_team, base_prob)
                prob = round(min(max(base_prob + owner_adj, 0.0), 1.0), 4)

                # The handedness split used as this matchup's BA base.
                _split = splits.get("vs_rhp" if pitcher_hand == "R" else "vs_lhp", {})

                candidates.append({
                    "batter_id":       batter_id,
                    "batter_name":     batter_name,
                    "split_avg":       _split.get("avg"),
                    "split_ab":        _split.get("ab", 0),
                    "park_factor":       runs_factor,
                    "pitcher_season_h9": _pitcher_hits_per_9(p_stats),
                    "pitcher_season_ip": p_stats.get("innings_pitched"),
                    "team":            team,
                    "opponent_team":   opponent_team,
                    "lineup_pos":      lineup_pos,
                    "pitcher_name":    pitcher_name,
                    "pitcher_hand":    pitcher_hand,
                    "h2h_ab":          h2h_stats.get("ab", 0),
                    "h2h_avg":         h2h_stats.get("avg"),
                    "recent_ab":       recent_stats.get("ab", 0),
                    "recent_avg":      recent_stats.get("avg"),
                    "pitcher_recent_h9": pitcher_recent.get("h_per_9"),
                    "pitcher_days_rest": pitcher_recent.get("days_rest"),
                    "team_recent_avg":   team_recent.get("avg"),
                    "is_day_game":       day_game,
                    "game_date":         game_slate_date(commence),   # keys the research row
                    "season_phase":      season_phase(game_slate_date(commence)),
                    "base_prob":       base_prob,
                    "owner_adj":       owner_adj,
                    "hit_probability": prob,
                    "calibrated_prob": calibrated_hit_prob(prob),
                    "game_pk":         game_pk,
                })

    candidates.sort(key=lambda c: c["hit_probability"], reverse=True)

    # Price the whole slate, before the research hand-off below, so research.db gets prices.
    _attach_hit_odds(candidates)

    # Every scored batter, for calibration research.
    if all_candidates_out is not None:
        all_candidates_out.extend(candidates)

    # Already ranked by edge, which is the same number as `ev` (both are leg_edge).
    top = _select_legs(candidates, HIT_PARLAY_LEGS)

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
        # Edge from the calibrated probability, via the gate's own leg_edge.
        leg["ev"] = leg_edge(leg.get("calibrated_prob"), entry["implied"])
    print(f"[hit_pipeline] matched book odds for {matched}/{len(legs)} scored batters "
          f"({len(board)} players on the board)")
    if board and matched < len(legs) * 0.5:
        print("[hit_pipeline] NOTE low odds coverage — if the board looks truncated, "
              "prop_odds._paginate stops at the provider's ~600-row cap")


def _select_legs(candidates: list[dict], max_legs: int) -> list[dict]:
    """Up to max_legs legs clearing the per-leg EV gate, at most 1 per team, ranked by
    edge against each leg's own price."""
    seen_teams: set[str] = set()
    legs: list[dict] = []
    rejected: dict[str, int] = {}

    def _edge_key(c: dict) -> float:
        # -inf, not `or -1`: an unpriced leg sorts last and an edge of 0.0 stays 0.0.
        edge = leg_edge(c.get("calibrated_prob"), c.get("book_implied"))
        return edge if edge is not None else float("-inf")

    ranked = sorted(candidates, key=_edge_key, reverse=True)

    for c in ranked:
        why = leg_gate_reason(c.get("calibrated_prob"), c.get("book_implied"),
                              c.get("season_phase"))
        if why:
            code, _detail = why
            rejected[code] = rejected.get(code, 0) + 1
            continue
        if c["team"] in seen_teams:
            rejected["one_per_team"] = rejected.get("one_per_team", 0) + 1
            continue
        seen_teams.add(c["team"])
        legs.append(c)
        if len(legs) >= max_legs:
            break

    if len(legs) < max_legs:
        detail = ", ".join(f"{code} {cnt}" for code, cnt in sorted(rejected.items()))
        print(f"[hit_pipeline] {len(legs)} of {max_legs} leg(s) cleared the EV gate "
              f"(min edge {MIN_EDGE_PTS:.1%}) — not padding with weaker legs. "
              f"Rejected: {detail or 'none'}")
    return legs


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
