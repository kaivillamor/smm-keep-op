import io
import json
import os
import requests
import pandas as pd
from datetime import datetime

MLB_API = "https://statsapi.mlb.com/api/v1"
SAVANT_BASE = "https://baseballsavant.mlb.com"


def fetch_stats() -> dict:
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    year = date_str[:4]

    probable = _fetch_probable_pitchers(date_str)
    pitcher_ids = {
        pid
        for game in probable.values()
        for pid in [game.get("home_pitcher_id"), game.get("away_pitcher_id")]
        if pid
    }

    pitcher_stats = {pid: _fetch_pitcher_season_stats(pid, year) for pid in pitcher_ids}
    savant = _fetch_savant_pitcher_leaderboard(year)

    for pid, stats in pitcher_stats.items():
        stats.update(savant.get(str(pid), {}))

    result = {
        "date": date_str,
        "probable_pitchers": probable,
        "pitcher_stats": pitcher_stats,
    }

    _save(result, date_str)
    print(f"[stats_fetcher] {len(probable)} games | {len(pitcher_stats)} pitchers fetched")
    return result


def _fetch_probable_pitchers(date_str: str) -> dict:
    url = f"{MLB_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "probablePitcher,team",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    probable = {}
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            home_pitcher = home.get("probablePitcher", {})
            away_pitcher = away.get("probablePitcher", {})

            probable[game["gamePk"]] = {
                "home_team": home["team"]["name"],
                "away_team": away["team"]["name"],
                "home_pitcher_id": home_pitcher.get("id"),
                "home_pitcher_name": home_pitcher.get("fullName"),
                "away_pitcher_id": away_pitcher.get("id"),
                "away_pitcher_name": away_pitcher.get("fullName"),
                "commence_time": game.get("gameDate"),
            }

    return probable


def _fetch_pitcher_season_stats(pitcher_id: int, year: str) -> dict:
    url = f"{MLB_API}/people/{pitcher_id}/stats"
    params = {
        "stats": "season",
        "group": "pitching",
        "season": year,
        "gameType": "R",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    try:
        stat = data["stats"][0]["splits"][0]["stat"]
        return {
            "era": float(stat.get("era", 0)),
            "whip": float(stat.get("whip", 0)),
            "k_per_9": float(stat.get("strikeoutsPer9Inn", 0)),
            "bb_per_9": float(stat.get("walksPer9Inn", 0)),
            "innings_pitched": float(stat.get("inningsPitched", 0)),
            "games_started": int(stat.get("gamesStarted", 0)),
        }
    except (IndexError, KeyError):
        return {}


def _fetch_savant_pitcher_leaderboard(year: str) -> dict:
    url = f"{SAVANT_BASE}/leaderboard/custom"
    params = {
        "year": year,
        "type": "pitcher",
        "filter": "",
        "sort": "4",
        "sortDir": "desc",
        "min": "1",
        "selections": "p_era,p_fip,xfip,p_k_percent,p_bb_percent,p_called_strike_percent",
        "player_type": "pitcher",
        "csv": "true",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))

    savant = {}
    for _, row in df.iterrows():
        pid = str(int(row.get("player_id", 0)))
        savant[pid] = {
            "fip": _safe_float(row.get("p_fip")),
            "xfip": _safe_float(row.get("xfip")),
            "k_pct": _safe_float(row.get("p_k_percent")),
            "bb_pct": _safe_float(row.get("p_bb_percent")),
        }

    return savant


def fetch_batter_statcast_season(year: str) -> dict:
    """
    Bulk-fetches Sweet Spot % and Hard Hit % for all batters from Baseball Savant leaderboard.
    One call for the whole season — use this to pre-filter before making per-batter calls.
    Returns dict keyed by player_id (str).
    """
    url = f"{SAVANT_BASE}/leaderboard/custom"
    params = {
        "year": year,
        "type": "batter",
        "filter": "",
        "sort": "4",
        "sortDir": "desc",
        "min": "1",
        "selections": "sweet_spot_percent,hard_hit_percent,xba,xwoba,barrel_batted_rate",
        "player_type": "batter",
        "csv": "true",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))

    result = {}
    for _, row in df.iterrows():
        pid = str(int(row.get("player_id", 0)))
        result[pid] = {
            "sweet_spot_percent": _safe_float(row.get("sweet_spot_percent")),
            "hard_hit_percent": _safe_float(row.get("hard_hit_percent")),
            "xba": _safe_float(row.get("xba")),
            "xwoba": _safe_float(row.get("xwoba")),
            "barrel_batted_rate": _safe_float(row.get("barrel_batted_rate")),
        }

    print(f"[stats_fetcher] Season batter Statcast: {len(result)} batters loaded")
    return result


def fetch_batter_recent_stats(batter_id: int, days: int = 14) -> dict:
    """
    Fetches Hard Hit % and Sweet Spot % for a batter over the last N days.
    Uses Baseball Savant statcast search aggregated by name over a date range.
    Returns empty dict if the batter had no batted balls in the window.
    """
    from datetime import timedelta
    today = datetime.utcnow().date()
    start = today - timedelta(days=days)

    url = f"{SAVANT_BASE}/statcast_search/csv"
    params = {
        "type": "batter",
        "player_id": batter_id,
        "start_dt": start.strftime("%Y-%m-%d"),
        "end_dt": today.strftime("%Y-%m-%d"),
        "hfGT": "R|",
        "group_by": "name",
        "sort_col": "pitches",
        "sort_order": "desc",
        "min_results": "0",
        "min_pas": "0",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()

    try:
        df = pd.read_csv(io.StringIO(resp.text))
        if df.empty:
            return {}
        row = df.iloc[0]
        n = row.get("n", None)
        return {
            "hard_hit_percent": _safe_float(row.get("hard_hit_percent")),
            "sweet_spot_percent": _safe_float(row.get("sweet_spot_percent")),
            "batted_balls": int(n) if n is not None and not pd.isna(n) else 0,
        }
    except Exception:
        return {}


def fetch_batter_zone_stats(batter_id: int, year: str) -> dict:
    """
    Fetches batter xwOBA by Statcast zone for the season.
    Pulls pitch-level data and aggregates estimated_woba_using_speedangle by zone.
    Returns {zone_id (int): xwoba (float)} for zones 1-14.
    """
    url = f"{SAVANT_BASE}/statcast_search/csv"
    params = {
        "type": "batter",
        "player_id": batter_id,
        "hfSea": f"{year}|",
        "hfGT": "R|",
        "sort_col": "game_date",
        "sort_order": "desc",
        "min_results": "0",
        "min_pas": "0",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()

    try:
        df = pd.read_csv(io.StringIO(resp.text))
        if df.empty or "zone" not in df.columns:
            return {}

        col = "estimated_woba_using_speedangle"
        if col not in df.columns:
            return {}

        valid = df[df["zone"].notna() & df[col].notna()].copy()
        if valid.empty:
            return {}

        valid["zone"] = valid["zone"].astype(int)
        return valid.groupby("zone")[col].mean().round(3).to_dict()
    except Exception:
        return {}


def fetch_pitcher_zone_tendencies(pitcher_id: int, year: str) -> dict:
    """
    Fetches a pitcher's pitch frequency by Statcast zone for the season.
    Returns {zone_id (int): fraction_of_total_pitches (float)}.
    """
    url = f"{SAVANT_BASE}/statcast_search/csv"
    params = {
        "type": "pitcher",
        "player_id": pitcher_id,
        "hfSea": f"{year}|",
        "hfGT": "R|",
        "sort_col": "game_date",
        "sort_order": "desc",
        "min_results": "0",
        "min_pas": "0",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()

    try:
        df = pd.read_csv(io.StringIO(resp.text))
        if df.empty or "zone" not in df.columns:
            return {}

        df_zones = df[df["zone"].notna()].copy()
        if df_zones.empty:
            return {}

        df_zones["zone"] = df_zones["zone"].astype(int)
        counts = df_zones["zone"].value_counts()
        total = counts.sum()
        return {int(z): round(count / total, 4) for z, count in counts.items()}
    except Exception:
        return {}


def fetch_batter_splits(batter_id: int, year: str = None) -> dict:
    if year is None:
        year = str(datetime.utcnow().year)

    url = f"{MLB_API}/people/{batter_id}/stats"
    params = {
        "stats": "statSplits",
        "group": "hitting",
        "season": year,
        "gameType": "R",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    splits = {"vs_rhp": {}, "vs_lhp": {}}
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            split_name = split.get("split", {}).get("description", "")
            stat = split.get("stat", {})
            if split_name == "vs. RHP":
                splits["vs_rhp"] = {
                    "avg": float(stat.get("avg", 0)),
                    "obp": float(stat.get("obp", 0)),
                    "slg": float(stat.get("slg", 0)),
                    "ops": float(stat.get("ops", 0)),
                    "ab": int(stat.get("atBats", 0)),
                }
            elif split_name == "vs. LHP":
                splits["vs_lhp"] = {
                    "avg": float(stat.get("avg", 0)),
                    "obp": float(stat.get("obp", 0)),
                    "slg": float(stat.get("slg", 0)),
                    "ops": float(stat.get("ops", 0)),
                    "ab": int(stat.get("atBats", 0)),
                }

    return splits


def _safe_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _save(data: dict, date_str: str) -> None:
    os.makedirs("data/stats", exist_ok=True)
    path = f"data/stats/{date_str}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[stats_fetcher] Saved → {path}")


if __name__ == "__main__":
    stats = fetch_stats()
    print(f"\nProbable pitchers today:\n")
    for game_id, game in stats["probable_pitchers"].items():
        home_p = game.get("home_pitcher_name", "TBD")
        away_p = game.get("away_pitcher_name", "TBD")
        print(f"  {game['away_team']} ({away_p}) @ {game['home_team']} ({home_p})")
