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

                # The handedness split actually used as this matchup's BA base. Stored
                # so a refit can use it: it is the model's single largest input, and it
                # was silently empty for every batter until the statSplits sitCodes fix
                # (2026-09-08), so rows logged before then have it NULL.
                _split = splits.get("vs_rhp" if pitcher_hand == "R" else "vs_lhp", {})

                candidates.append({
                    "batter_id":       batter_id,
                    "batter_name":     batter_name,
                    "split_avg":       _split.get("avg"),
                    "split_ab":        _split.get("ab", 0),
                    # Needed to REPRODUCE the model from stored rows. Without these the
                    # reconstruction runs on 100% recent H/9 (the model blends season
                    # 60/40) and on a neutral park, giving 0.05 mean error and making
                    # any weight-fitting describe a different model than the live one.
                    "park_factor":       runs_factor,
                    "pitcher_season_h9": _pitcher_hits_per_9(p_stats),
                    # The sample the shrinkage weight is computed from. §24 — h9 is now
                    # regressed toward league average by innings, so without this the
                    # stored h9 cannot be reproduced or un-shrunk by a later refit.
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
                    # Venue lookup removed 2026-09-08 — the API ignored venueId, so this
                    # was the batter's career line, not a park split. Logged as NULL now;
                    # park effects come from park_factors. Saves ~150 API calls per run.
                    "venue_ab":          None,
                    "venue_avg":         None,
                    "pitcher_recent_h9": pitcher_recent.get("h_per_9"),
                    "pitcher_days_rest": pitcher_recent.get("days_rest"),
                    "team_recent_avg":   team_recent.get("avg"),
                    "is_day_game":       day_game,
                    # The slate this GAME belongs to, not the day collection happened to
                    # run. Keys the research row — see game_slate_date() and §29.
                    "game_date":         game_slate_date(commence),
                    # Which regime this game falls in. The EV gate refuses to fire outside
                    # the phase the calibration was fit on — see hit_model.CALIBRATION_PHASE.
                    "season_phase":      season_phase(game_slate_date(commence)),
                    "base_prob":       base_prob,
                    "owner_adj":       owner_adj,
                    "hit_probability": prob,
                    # Raw value above is what research.db logs. The calibrated one below
                    # is what the betting gate uses — see hit_model.calibrated_hit_prob.
                    "calibrated_prob": calibrated_hit_prob(prob),
                    "game_pk":         game_pk,
                })

    candidates.sort(key=lambda c: c["hit_probability"], reverse=True)

    # Price the WHOLE slate, not just the surfaced legs. Costs NOTHING extra:
    # fetch_hit_prop_odds() already pulls the entire board in one paginated call
    # (~180 players) and the old code looked up 4 names from it, discarding the rest.
    # Pricing only the top 4 made the recorded price distribution a 4-row sample of the
    # model's own favourites — which cannot answer whether playable prices exist on the
    # legs it likes, and that question decides whether this layer is viable at all.
    # No legs are dropped for negative EV — we surface the same picks, ranked by edge.
    _attach_hit_odds(candidates)

    # Optional hand-off of EVERY scored batter for calibration research. The surfaced
    # legs alone are a biased sample (the ones the model already liked), which can't
    # reveal whether the ranking works. Default None keeps existing callers unchanged.
    # Odds are attached ABOVE this line so research.db records the price too.
    if all_candidates_out is not None:
        all_candidates_out.extend(candidates)

    top = _select_legs(candidates, HIT_PARLAY_LEGS)
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
        # EV as a probability edge: how much higher our estimate is than the book's.
        # Uses the CALIBRATED probability, not the raw one — EV is a betting decision and
        # the raw number is known to overstate badly at the top end. Computing edge from
        # the inflated value manufactured positive EV that was never there.
        #
        # Delegates to hit_model.leg_edge so this and the gate can never disagree. It had
        # its own copy of the subtraction, and two formulas for one quantity is how they
        # drift — note the old fallback to the RAW probability when calibrated_prob was
        # missing, which would have reintroduced exactly the overstatement above.
        leg["ev"] = leg_edge(leg.get("calibrated_prob"), entry["implied"])
    print(f"[hit_pipeline] matched book odds for {matched}/{len(legs)} scored batters "
          f"({len(board)} players on the board)")
    if board and matched < len(legs) * 0.5:
        # Low coverage has two very different causes: names that failed to normalise, or
        # players absent because _paginate hit the provider's ~600-row cap. Say which.
        print("[hit_pipeline] NOTE low odds coverage — if the board looks truncated, "
              "prop_odds._paginate stops at the provider's ~600-row cap")


def _select_legs(candidates: list[dict], max_legs: int) -> list[dict]:
    """Pick up to max_legs legs clearing the per-leg EV gate, at most 1 per team.

    Selection is on EDGE against each leg's OWN price, not on a probability threshold.
    A single threshold derived from the median price cannot protect against selection,
    because the legs the model likes are not priced at the median — measured, and then
    confirmed live when the old gate selected legs that did WORSE than the ones it
    rejected (§29, hit_model.MIN_EDGE_PTS).

    Prices are attached to the whole slate before this runs, so every candidate already
    carries book_implied wherever the board had one.

    Ranked by edge, highest first, so a thin slate keeps the best available rather than
    whatever the lineup order happened to put first.
    """
    seen_teams: set[str] = set()
    legs: list[dict] = []
    rejected: dict[str, int] = {}

    ranked = sorted(
        candidates,
        key=lambda c: (leg_edge(c.get("calibrated_prob"), c.get("book_implied"))
                       if leg_edge(c.get("calibrated_prob"), c.get("book_implied"))
                          is not None else float("-inf")),
        reverse=True)

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
        # Say WHY nothing qualified. An unexplained empty slate is indistinguishable from
        # a broken gate, which is exactly how the 2026-09-09 gate stayed dead for three
        # weeks — it surfaced nothing and that read as a fact about the market.
        detail = ", ".join(f"{code} {cnt}" for code, cnt in sorted(rejected.items()))
        print(f"[hit_pipeline] {len(legs)} of {max_legs} leg(s) cleared the EV gate "
              f"(min edge {MIN_EDGE_PTS:.1%}) — not padding with weaker legs. "
              f"Rejected: {detail or 'none'}")
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
