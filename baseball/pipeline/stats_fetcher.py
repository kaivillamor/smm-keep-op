import io
import json
import math
import os
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

MLB_API = "https://statsapi.mlb.com/api/v1"
SAVANT_BASE = "https://baseballsavant.mlb.com"


def fetch_stats() -> dict:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    year = date_str[:4]

    probable = _fetch_probable_pitchers(date_str)
    pitcher_ids = {
        pid
        for game in probable.values()
        for pid in [game.get("home_pitcher_id"), game.get("away_pitcher_id")]
        if pid
    }

    pitcher_stats = {str(pid): _fetch_pitcher_season_stats(pid, year) for pid in pitcher_ids}
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
                "home_pitcher_throws": (home_pitcher.get("pitchHand") or {}).get("code", "R"),
                "away_pitcher_id": away_pitcher.get("id"),
                "away_pitcher_name": away_pitcher.get("fullName"),
                "away_pitcher_throws": (away_pitcher.get("pitchHand") or {}).get("code", "R"),
                "commence_time": game.get("gameDate"),
            }

    return probable


_FIP_CONSTANT = 3.12   # league-average FIP constant (roughly stable year-to-year)


def _parse_ip(ip_str) -> float:
    """Convert '78.1' (78 and 1/3 innings) to 78.333."""
    try:
        s = str(ip_str)
        if "." in s:
            whole, thirds = s.split(".", 1)
            return int(whole) + int(thirds) / 3
        return float(s)
    except (ValueError, TypeError):
        return 0.0


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
        ip = _parse_ip(stat.get("inningsPitched", 0))
        hr = int(stat.get("homeRuns", 0))
        bb = int(stat.get("baseOnBalls", 0))
        hbp = int(stat.get("hitByPitch", 0))
        k = int(stat.get("strikeOuts", 0))

        # FIP = ((13*HR) + (3*(BB+HBP)) - (2*K)) / IP + constant
        fip = round((13 * hr + 3 * (bb + hbp) - 2 * k) / ip + _FIP_CONSTANT, 2) if ip > 0 else None

        return {
            "era": _safe_float(stat.get("era")),
            "whip": _safe_float(stat.get("whip")),
            "fip": fip,
            "k_per_9": _safe_float(stat.get("strikeoutsPer9Inn")),
            "bb_per_9": _safe_float(stat.get("walksPer9Inn")),
            "opp_avg": _safe_float(stat.get("avg")),
            "innings_pitched": ip,
            "games_started": int(stat.get("gamesStarted", 0)),
        }
    except (IndexError, KeyError):
        return {}


def _fetch_savant_pitcher_leaderboard(year: str) -> dict:
    """
    Fetches xERA (expected ERA from Statcast contact quality) for all pitchers.
    The custom leaderboard p_fip/xfip columns are empty; expected_statistics has real values.
    xERA is used as our xfip proxy in _pitcher_run_delta.
    """
    url = f"{SAVANT_BASE}/leaderboard/expected_statistics"
    params = {
        "type": "pitcher",
        "year": year,
        "position": "",
        "team": "",
        "csv": "true",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))

    savant = {}
    for _, row in df.iterrows():
        pid = str(int(row.get("player_id", 0)))
        savant[pid] = {
            "xfip": _safe_float(row.get("xera")),   # xERA ≈ xFIP: both normalize ERA for true contact quality
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
    Pulls raw batted ball events and computes metrics from launch_speed / launch_angle
    to avoid column name uncertainty in Statcast's group_by aggregation.
    Returns empty dict if the batter had no batted balls in the window.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    url = f"{SAVANT_BASE}/statcast_search/csv"
    params = {
        "type": "batter",
        "player_id": batter_id,
        "hfBBT": "ground_ball|line_drive|fly_ball|popup|",
        "start_dt": start.strftime("%Y-%m-%d"),
        "end_dt": today.strftime("%Y-%m-%d"),
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
        if df.empty:
            return {}

        result: dict = {"batted_balls": len(df)}

        if "launch_speed" in df.columns:
            ev = df["launch_speed"].dropna()
            if len(ev) > 0:
                result["hard_hit_percent"] = round((ev >= 95).sum() / len(ev) * 100, 1)

        if "launch_angle" in df.columns:
            la = df["launch_angle"].dropna()
            if len(la) > 0:
                result["sweet_spot_percent"] = round(((la >= 8) & (la <= 32)).sum() / len(la) * 100, 1)

        return result
    except Exception as e:
        print(f"[stats_fetcher] fetch_batter_recent_stats({batter_id}): {e}")
        return {}


def fetch_batter_zone_stats(batter_id: int, year: str) -> dict:
    """
    Fetches batter xwOBA by Statcast zone for the season.
    Filters to batted balls only (hfBBT) so estimated_woba_using_speedangle is populated
    and the dataset is smaller. Aggregates xwOBA by zone.
    Returns {zone_id (int): xwoba (float)} for zones 1-14.
    """
    url = f"{SAVANT_BASE}/statcast_search/csv"
    params = {
        "type": "batter",
        "player_id": batter_id,
        "hfSea": f"{year}|",
        "hfBBT": "ground_ball|line_drive|fly_ball|popup|",
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
    except Exception as e:
        print(f"[stats_fetcher] fetch_batter_zone_stats({batter_id}): {e}")
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
    except Exception as e:
        print(f"[stats_fetcher] fetch_pitcher_zone_tendencies({pitcher_id}): {e}")
        return {}


def fetch_batter_splits(batter_id: int, year: str = None) -> dict:
    if year is None:
        year = str(datetime.now(timezone.utc).year)

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
        f = float(val)
        return None if math.isnan(f) else f
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
