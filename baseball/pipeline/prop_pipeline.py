from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from pipeline.stats_fetcher import (
    fetch_batter_recent_stats,
    fetch_batter_zone_stats,
    fetch_pitcher_zone_tendencies,
)
from model.factors.hr_prop_model import score_batter_hr_props, rank_score, RECENT_DAYS, BARREL_THRESHOLD

# Slightly under the final gate thresholds
_SEASON_PREFILTER        = 55.0
_BARREL_PREFILTER        = BARREL_THRESHOLD - 3.0  # catches borderline barrel + pitcher_hrfb gate guys
_TOP_N                   = 5    # max candidates to surface after gate + ranking (2/18 hit at top-8 vs ~20% break-even)

# Savant's p_home_run_pct leaderboard returns all NaN in 2026. Proxy HR/FB from
# FIP-xFIP gap instead: a pitcher allowing more HRs than expected has FIP > xFIP.
# Calibration: ~4 pts of HR/FB per 1.0 FIP-xFIP unit above league avg (11%).
_LEAGUE_AVG_HRFB = 11.0
_HRFB_PER_FIP_UNIT = 4.0


def _pitcher_hrfb_proxy(p_stats: dict) -> float | None:
    fip  = p_stats.get("fip")
    xfip = p_stats.get("xfip")
    if fip is None or xfip is None:
        return None
    return round(_LEAGUE_AVG_HRFB + (fip - xfip) * _HRFB_PER_FIP_UNIT, 1)


def analyze_hr_props(
    lineups: dict,
    season_batter_stats: dict,
    probable_pitchers: dict,
    pitcher_stats: dict | None = None,
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

    # Collect the unique probable-pitcher IDs across confirmed games (no network).
    pitcher_ids: set[int] = set()
    for game_pk, lineup_entry in lineups.items():
        if not lineup_entry.get("confirmed"):
            continue
        probable = _match_probable(game_pk, probable_pitchers)
        for pid, batters in (
            (probable.get("away_pitcher_id"), lineup_entry.get("home_lineup", [])),
            (probable.get("home_pitcher_id"), lineup_entry.get("away_lineup", [])),
        ):
            if pid and batters:
                pitcher_ids.add(pid)

    # Fetch pitcher zone tendencies in parallel — sequentially these can stall the
    # whole run when Savant is slow (each retry is up to ~65s). Cached by pitcher_id.
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

    # Build the work queue: one entry per (batter, pitcher, side, game) that passes pre-filter.
    work_items = []
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

            pitcher_zones = pitcher_zone_cache.get(pitcher_id, {})
            p_stats       = (pitcher_stats or {}).get(str(pitcher_id), {})
            hr_fb_rate    = _pitcher_hrfb_proxy(p_stats)

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

    print(f"[prop_pipeline] Batters checked: {total_batters_checked} | "
          f"Passed pre-filter: {prefilter_passed} | "
          f"Passed gate: {len(candidates)} | Surfaced: {len(top)}")
    return top


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
