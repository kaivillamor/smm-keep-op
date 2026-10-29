from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from pipeline.stats_fetcher import (
    fetch_batter_recent_stats,
    fetch_batter_zone_stats,
    fetch_pitcher_xera,
    fetch_pitcher_zone_tendencies,
    _match_probable,
)
from model.factors.hr_prop_model import score_batter_hr_props, rank_score, RECENT_DAYS, BARREL_THRESHOLD
from pipeline.prop_odds import fetch_hr_prop_odds, normalize_name
from pipeline.stats_fetcher import game_has_started

# Slightly under the final gate thresholds
_SEASON_PREFILTER        = 55.0
_BARREL_PREFILTER        = BARREL_THRESHOLD - 3.0  # catches borderline barrel + pitcher_hrfb gate guys
_TOP_N                   = 5    # max candidates to surface after gate + ranking (2/18 hit at top-8 vs ~20% break-even)

# Savant's HR/FB leaderboard is empty for 2026, so HR/FB is proxied from FIP − xERA
# (FIP above xERA reads as homer-prone): ~4 pts of HR/FB per unit above league avg (11%).
_LEAGUE_AVG_HRFB = 11.0
_HRFB_PER_FIP_UNIT = 4.0


def _pitcher_hrfb_proxy(p_stats: dict) -> float | None:
    """Estimate HR/FB from the FIP − xERA gap, clamped at 0."""
    fip  = p_stats.get("fip")
    xera = p_stats.get("xera")
    if fip is None or xera is None:
        return None
    proxy = _LEAGUE_AVG_HRFB + (fip - xera) * _HRFB_PER_FIP_UNIT
    return round(max(proxy, 0.0), 1)


def analyze_hr_props(
    lineups: dict,
    season_batter_stats: dict,
    probable_pitchers: dict,
    pitcher_stats: dict | None = None,
) -> list[dict]:
    """
    Runs the HR gate (hr_prop_model.score_batter_hr_props, four ways to pass) across
    every batter in confirmed lineups.

    lineups              : from fetch_lineups()
    season_batter_stats  : from fetch_batter_statcast_season()
    probable_pitchers    : from stats["probable_pitchers"]
    pitcher_stats        : from stats["pitcher_stats"] — supplies FIP for the HR/FB proxy

    Returns a list of candidates who pass the gate, each with batter info + scores.
    """
    year = str(datetime.now(timezone.utc).year)
    candidates = []
    pitcher_zone_cache: dict[int, dict] = {}

    confirmed_games = sum(1 for v in lineups.values() if v.get("confirmed"))
    total_batters_checked = 0
    prefilter_passed = 0

    # Games both confirmed and not yet started.
    eligible = {
        pk: entry for pk, entry in lineups.items()
        if entry.get("confirmed") and not game_has_started(entry.get("commence_time"))
    }
    started = confirmed_games - len(eligible)
    print(f"[prop_pipeline] {confirmed_games}/{len(lineups)} games have confirmed lineups"
          + (f" | {started} already started → {len(eligible)} bettable" if started else ""))

    # Collect the unique probable-pitcher IDs across eligible games (no network).
    pitcher_ids: set[int] = set()
    for game_pk, lineup_entry in eligible.items():
        probable = _match_probable(game_pk, probable_pitchers) or {}
        for pid, batters in (
            (probable.get("away_pitcher_id"), lineup_entry.get("home_lineup", [])),
            (probable.get("home_pitcher_id"), lineup_entry.get("away_lineup", [])),
        ):
            if pid and batters:
                pitcher_ids.add(pid)

    # Pitcher zone tendencies, fetched in parallel (Savant is slow).
    print(f"[prop_pipeline] Fetching zone tendencies for {len(pitcher_ids)} pitcher(s)...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_pitcher_zone_tendencies, pid, year): pid
                   for pid in pitcher_ids}
        for future in as_completed(futures):
            pid = futures[future]
            try:
                pitcher_zone_cache[pid] = future.result()
            except Exception as e:
                print(f"[prop_pipeline] pitcher zone fetch error ({pid}): {e}")
                pitcher_zone_cache[pid] = {}

    # xERA for the HR/FB proxy; empty on failure, which turns gate condition 4 off.
    xera = fetch_pitcher_xera(year) if pitcher_ids else {}

    # Build the work queue: one entry per (batter, pitcher, side, game) that passes pre-filter.
    work_items = []
    for game_pk, lineup_entry in eligible.items():
        probable = _match_probable(game_pk, probable_pitchers) or {}
        batter_groups = [
            (lineup_entry.get("home_lineup", []), probable.get("away_pitcher_id"), "home"),
            (lineup_entry.get("away_lineup", []), probable.get("home_pitcher_id"), "away"),
        ]

        for batters, pitcher_id, side in batter_groups:
            if not pitcher_id or not batters:
                continue

            pitcher_zones = pitcher_zone_cache.get(pitcher_id, {})
            p_stats       = (pitcher_stats or {}).get(str(pitcher_id), {})
            hr_fb_rate    = _pitcher_hrfb_proxy({**p_stats, **xera.get(str(pitcher_id), {})})

            for batter in batters:
                batter_id = batter.get("id")
                if not batter_id:
                    continue
                total_batters_checked += 1
                season_stats = season_batter_stats.get(str(batter_id), {})
                if not _passes_season_prefilter(season_stats):
                    continue
                prefilter_passed += 1
                team_key = "home_team" if side == "home" else "away_team"
                work_items.append({
                    "batter_id":    batter_id,
                    "batter_name":  batter.get("name"),
                    "team":         lineup_entry.get(team_key),
                    "game_pk":      game_pk,
                    "pitcher_id":   pitcher_id,
                    "pitcher_zones": pitcher_zones,
                    "hr_fb_rate":   hr_fb_rate,
                    "season_stats": season_stats,
                })

    # Fetch per-batter data in parallel (recent stats + zone stats are independent).
    def _fetch_batter_data(item: dict) -> dict:
        bid          = item["batter_id"]
        recent_stats = fetch_batter_recent_stats(bid, days=RECENT_DAYS)
        batter_zones = fetch_batter_zone_stats(bid, year)
        return {**item, "recent_stats": recent_stats, "batter_zones": batter_zones}

    print(f"[prop_pipeline] Fetching recent/zone data for {len(work_items)} batter(s)...")
    fetched = []
    done = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_batter_data, w): w for w in work_items}
        for future in as_completed(futures):
            done += 1
            try:
                fetched.append(future.result())
            except Exception as e:
                print(f"[prop_pipeline] batter fetch error: {e}")
            if done % 10 == 0 or done == len(work_items):
                print(f"[prop_pipeline]   ...{done}/{len(work_items)} batters fetched")

    for item in fetched:
        scores = score_batter_hr_props(
            item["season_stats"], item["recent_stats"],
            item["batter_zones"], item["pitcher_zones"],
            pitcher_hr_fb=item["hr_fb_rate"],
        )
        if scores["passes_gate"]:
            candidates.append({
                "batter_id":   item["batter_id"],
                "batter_name": item["batter_name"],
                "team":        item["team"],
                "game_pk":     item["game_pk"],
                "pitcher_id":  item["pitcher_id"],
                "scores":      scores,
            })

    # Rank by composite score, surface only the top N
    for c in candidates:
        c["rank_score"] = rank_score(c["scores"])
    candidates.sort(key=lambda c: c["rank_score"], reverse=True)
    top = candidates[:_TOP_N]

    _attach_hr_odds(top)

    print(f"[prop_pipeline] Batters checked: {total_batters_checked} | "
          f"Passed pre-filter: {prefilter_passed} | "
          f"Passed gate: {len(candidates)} | Surfaced: {len(top)}")
    return top


def _attach_hr_odds(cands: list[dict]) -> None:
    """Attach each candidate's 1+ HR odds (player_home_runs market) by name,
    setting book_odds / book_implied / book in place. Unmatched → None."""
    if not cands:
        return
    board = fetch_hr_prop_odds()
    matched = 0
    for c in cands:
        entry = board.get(normalize_name(c.get("batter_name", "")))
        if entry:
            matched += 1
            c["book_odds"]    = entry["odds"]
            c["book_implied"] = entry["implied"]
            c["book"]         = entry["book"]
        else:
            c["book_odds"] = c["book_implied"] = c["book"] = None
    print(f"[prop_pipeline] matched HR odds for {matched}/{len(cands)} candidates")


def _passes_season_prefilter(season_stats: dict) -> bool:
    """
    Quick check before spending API calls on recent/zone data.
    Passes if the batter could plausibly qualify via any gate condition:
      - barrel rate alone (>= BARREL_PREFILTER)
      - sweet spot or hard hit at season level (>= _SEASON_PREFILTER)
    Zone Fit can't be pre-filtered — it's a per-matchup calculation.
    """
    barrel     = season_stats.get("barrel_batted_rate")
    sweet_spot = season_stats.get("sweet_spot_percent")
    hard_hit   = season_stats.get("hard_hit_percent")

    if barrel is not None and barrel >= _BARREL_PREFILTER:
        return True
    if sweet_spot is not None and sweet_spot >= _SEASON_PREFILTER:
        return True
    if hard_hit is not None and hard_hit >= _SEASON_PREFILTER:
        return True
    return False
