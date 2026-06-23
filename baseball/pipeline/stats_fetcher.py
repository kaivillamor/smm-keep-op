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
    savant   = _fetch_savant_pitcher_leaderboard(year)
    hr_fb    = _fetch_savant_pitcher_hr_fb(year)

    for pid, stats in pitcher_stats.items():
        stats.update(savant.get(str(pid), {}))
        stats.update(hr_fb.get(str(pid), {}))

    standings = fetch_standings(year)
    wrc_plus  = fetch_team_wrc_plus(year)

    result = {
        "date": date_str,
        "probable_pitchers": probable,
        "pitcher_stats": pitcher_stats,
        "standings": standings,
        "wrc_plus": wrc_plus,
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


def _fetch_savant_pitcher_hr_fb(year: str) -> dict:
    """
    Fetches HR/FB rate for all pitchers from Baseball Savant custom leaderboard.
    HR/FB = home runs allowed per fly ball — higher means more homer-prone.
    League average is ~10-12%. Returns {player_id (str): {"hr_fb_rate": float}}.
    """
    url = f"{SAVANT_BASE}/leaderboard/custom"
    params = {
        "year": year,
        "type": "pitcher",
        "filter": "",
        "sort": "4",
        "sortDir": "desc",
        "min": "1",
        "selections": "p_home_run_pct",
        "player_type": "pitcher",
        "csv": "true",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))

        col = next((c for c in df.columns if "home_run" in c.lower()), None)
        if not col:
            print(f"[stats_fetcher] HR/FB column not found in Savant pitcher leaderboard. Columns: {list(df.columns[:10])}")
            return {}

        result = {}
        for _, row in df.iterrows():
            pid = str(int(row.get("player_id", 0)))
            val = _safe_float(row.get(col))
            if val is not None:
                result[pid] = {"hr_fb_rate": round(val, 1)}
        return result
    except Exception as e:
        print(f"[stats_fetcher] _fetch_savant_pitcher_hr_fb: {e}")
        return {}


def fetch_standings(year: str) -> dict:
    """
    Fetches current season win% and run differential for all 30 teams.
    Returns {team_name: {win_pct, run_diff, wins, losses}}.
    Falls back to empty dict on failure — probability model uses 0.0 quality adj.
    """
    url = f"{MLB_API}/standings"
    params = {
        "leagueId": "103,104",
        "season": year,
        "standingsType": "regularSeason",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        standings = {}
        for record in data.get("records", []):
            for tr in record.get("teamRecords", []):
                name     = tr["team"]["name"]
                wins     = tr.get("wins", 0)
                losses   = tr.get("losses", 0)
                total    = wins + losses
                run_diff = tr.get("runDifferential", 0)
                standings[name] = {
                    "win_pct":          round(wins / total, 4) if total > 0 else 0.500,
                    "run_diff":         run_diff,
                    "run_diff_per_game": round(run_diff / total, 3) if total > 0 else 0.0,
                    "wins":             wins,
                    "losses":           losses,
                }

        print(f"[stats_fetcher] Standings: {len(standings)} teams loaded")
        return standings
    except Exception as e:
        print(f"[stats_fetcher] fetch_standings: {e}")
        return {}


_FG_ABBR_TO_TEAM: dict[str, str] = {
    "LAA": "Los Angeles Angels",
    "HOU": "Houston Astros",
    "OAK": "Oakland Athletics",
    "ATH": "Oakland Athletics",
    "TOR": "Toronto Blue Jays",
    "ATL": "Atlanta Braves",
    "MIL": "Milwaukee Brewers",
    "STL": "St. Louis Cardinals",
    "CHC": "Chicago Cubs",
    "ARI": "Arizona Diamondbacks",
    "LAD": "Los Angeles Dodgers",
    "SF":  "San Francisco Giants",
    "SFG": "San Francisco Giants",
    "CLE": "Cleveland Guardians",
    "MIA": "Miami Marlins",
    "NYM": "New York Mets",
    "WSH": "Washington Nationals",
    "WSN": "Washington Nationals",
    "BAL": "Baltimore Orioles",
    "SD":  "San Diego Padres",
    "SDP": "San Diego Padres",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "TEX": "Texas Rangers",
    "TB":  "Tampa Bay Rays",
    "TBR": "Tampa Bay Rays",
    "BOS": "Boston Red Sox",
    "CIN": "Cincinnati Reds",
    "COL": "Colorado Rockies",
    "KC":  "Kansas City Royals",
    "KCR": "Kansas City Royals",
    "SEA": "Seattle Mariners",
    "DET": "Detroit Tigers",
    "MIN": "Minnesota Twins",
    "CWS": "Chicago White Sox",
    "CHW": "Chicago White Sox",
    "NYY": "New York Yankees",
}


def fetch_team_wrc_plus(year: str) -> dict:
    """
    Fetches team wRC+ from FanGraphs dashboard leaderboard.
    wRC+ is park and league adjusted: 100 = league avg, 110 = 10% above avg.
    Returns {team_full_name: wrc_plus (float)}.
    Falls back to empty dict on failure.
    """
    url = "https://www.fangraphs.com/leaders.aspx"
    params = {
        "pos":     "all",
        "stats":   "bat",
        "lg":      "all",
        "qual":    "0",
        "type":    "8",
        "season":  year,
        "season1": year,
        "ind":     "0",
        "team":    "0,ts",
        "wal":     "0",
        "csv":     "1",
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; mlb-research-tool/1.0)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))

        # FanGraphs CSV column is "wRC+" — find it case-insensitively
        wrc_col = next((c for c in df.columns if c.strip().lower() == "wrc+"), None)
        team_col = next((c for c in df.columns if c.strip().lower() == "team"), None)
        if not wrc_col or not team_col:
            print(f"[stats_fetcher] FanGraphs wRC+ columns not found. Got: {list(df.columns[:10])}")
            return {}

        result = {}
        for _, row in df.iterrows():
            abbr    = str(row[team_col]).strip()
            full    = _FG_ABBR_TO_TEAM.get(abbr)
            wrc_val = _safe_float(row[wrc_col])
            if full and wrc_val is not None:
                result[full] = wrc_val

        print(f"[stats_fetcher] FanGraphs wRC+: {len(result)} teams loaded")
        return result
    except Exception as e:
        print(f"[stats_fetcher] fetch_team_wrc_plus: {e}")
        return {}


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
    Uses start_dt/end_dt instead of hfSea — hfSea is unreliable in the statcast_search endpoint.
    """
    today = datetime.now(timezone.utc).date()
    url = f"{SAVANT_BASE}/statcast_search/csv"
    params = {
        "all": "true",
        "type": "batter",
        "player_id": batter_id,
        "start_dt": f"{year}-01-01",
        "end_dt": today.strftime("%Y-%m-%d"),
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
    Uses start_dt/end_dt instead of hfSea — hfSea is unreliable in the statcast_search endpoint.
    """
    today = datetime.now(timezone.utc).date()
    url = f"{SAVANT_BASE}/statcast_search/csv"
    params = {
        "all": "true",
        "type": "pitcher",
        "player_id": pitcher_id,
        "start_dt": f"{year}-01-01",
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


# MLB team IDs — stable, update only when expansion teams are added.
_TEAM_IDS: dict[str, int] = {
    "Arizona Diamondbacks":  109,
    "Atlanta Braves":        144,
    "Baltimore Orioles":     110,
    "Boston Red Sox":        111,
    "Chicago Cubs":          112,
    "Chicago White Sox":     145,
    "Cincinnati Reds":       113,
    "Cleveland Guardians":   114,
    "Colorado Rockies":      115,
    "Detroit Tigers":        116,
    "Houston Astros":        117,
    "Kansas City Royals":    118,
    "Los Angeles Angels":    108,
    "Los Angeles Dodgers":   119,
    "Miami Marlins":         146,
    "Milwaukee Brewers":     158,
    "Minnesota Twins":       142,
    "New York Mets":         121,
    "New York Yankees":      147,
    "Oakland Athletics":     133,
    "Philadelphia Phillies": 143,
    "Pittsburgh Pirates":    134,
    "San Diego Padres":      135,
    "San Francisco Giants":  137,
    "Seattle Mariners":      136,
    "St. Louis Cardinals":   138,
    "Tampa Bay Rays":        139,
    "Texas Rangers":         140,
    "Toronto Blue Jays":     141,
    "Washington Nationals":  120,
}


def get_team_id(team_name: str) -> int | None:
    return _TEAM_IDS.get(team_name)


# MLB venue IDs — stable, update only when a team moves or opens a new park.
_VENUE_IDS: dict[str, int] = {
    "Arizona Diamondbacks":  15,
    "Atlanta Braves":        4705,
    "Baltimore Orioles":     2,
    "Boston Red Sox":        3,
    "Chicago Cubs":          17,
    "Chicago White Sox":     4,
    "Cincinnati Reds":       2602,
    "Cleveland Guardians":   5,
    "Colorado Rockies":      19,
    "Detroit Tigers":        2394,
    "Houston Astros":        2392,
    "Kansas City Royals":    7,
    "Los Angeles Angels":    1,
    "Los Angeles Dodgers":   22,
    "Miami Marlins":         4169,
    "Milwaukee Brewers":     32,
    "Minnesota Twins":       3312,
    "New York Mets":         3289,
    "New York Yankees":      3313,
    "Oakland Athletics":     10,
    "Philadelphia Phillies": 2681,
    "Pittsburgh Pirates":    31,
    "San Diego Padres":      2680,
    "San Francisco Giants":  2395,
    "Seattle Mariners":      680,
    "St. Louis Cardinals":   2889,
    "Tampa Bay Rays":        12,
    "Texas Rangers":         5325,
    "Toronto Blue Jays":     14,
    "Washington Nationals":  3309,
}


def get_venue_id(home_team: str) -> int | None:
    return _VENUE_IDS.get(home_team)


def fetch_batter_venue_stats(batter_id: int, venue_id: int) -> dict:
    """
    Career regular-season batting stats for a batter at a specific MLB ballpark.
    Display-only — not weighted into hit probability.
    Returns {ab, hits, avg}. Empty dict if no history at this venue.
    """
    url = f"{MLB_API}/people/{batter_id}/stats"
    params = {
        "stats":   "career",
        "group":   "hitting",
        "gameType": "R",
        "venueId": venue_id,
    }
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        stat = resp.json()["stats"][0]["splits"][0]["stat"]
        return {
            "ab":   int(stat.get("atBats", 0)),
            "hits": int(stat.get("hits", 0)),
            "avg":  _safe_float(stat.get("avg")),
        }
    except (IndexError, KeyError, requests.RequestException):
        return {}


def fetch_pitcher_recent_form(pitcher_id: int, num_starts: int = 3) -> dict:
    """
    Computes H/9 and days rest from the pitcher's last N starts via the game log.
    Returns {h_per_9, days_rest}. Empty dict if no starts logged yet this season.
    """
    year = str(datetime.now(timezone.utc).year)
    url  = f"{MLB_API}/people/{pitcher_id}/stats"
    params = {
        "stats":    "gameLog",
        "group":    "pitching",
        "season":   year,
        "gameType": "R",
    }
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        splits = resp.json()["stats"][0]["splits"]

        starts = [s for s in splits if int(s["stat"].get("gamesStarted", 0)) > 0]
        if not starts:
            return {}

        recent = starts[-num_starts:]

        total_h  = sum(int(s["stat"].get("hits", 0)) for s in recent)
        total_ip = sum(_parse_ip(s["stat"].get("inningsPitched", 0)) for s in recent)
        h_per_9  = round(total_h / total_ip * 9, 2) if total_ip > 0 else None

        last_date_str = recent[-1].get("date", "")
        days_rest = None
        if last_date_str:
            try:
                last_date = datetime.strptime(last_date_str[:10], "%Y-%m-%d").date()
                days_rest = (datetime.now(timezone.utc).date() - last_date).days
            except ValueError:
                pass

        return {"h_per_9": h_per_9, "days_rest": days_rest}
    except (IndexError, KeyError, requests.RequestException):
        return {}


def fetch_team_recent_hitting(team_id: int, days: int = 14) -> dict:
    """
    Team batting average over the last N days from the MLB Stats API.
    Returns {avg}. Empty dict if no data available.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    url    = f"{MLB_API}/teams/{team_id}/stats"
    params = {
        "stats":     "byDateRange",
        "group":     "hitting",
        "season":    str(today.year),
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate":   today.strftime("%Y-%m-%d"),
        "gameType":  "R",
    }
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        stat = resp.json()["stats"][0]["splits"][0]["stat"]
        return {"avg": _safe_float(stat.get("avg"))}
    except (IndexError, KeyError, requests.RequestException):
        return {}


def fetch_batter_vs_pitcher(batter_id: int, pitcher_id: int) -> dict:
    """
    Career regular-season stats for a batter against one specific pitcher.
    Returns {ab, hits, avg}. Empty dict if no history exists.
    """
    url = f"{MLB_API}/people/{batter_id}/stats"
    params = {
        "stats": "vsPlayer",
        "opposingPlayerId": pitcher_id,
        "group": "hitting",
        "gameType": "R",
    }
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        stat = resp.json()["stats"][0]["splits"][0]["stat"]
        return {
            "ab":   int(stat.get("atBats", 0)),
            "hits": int(stat.get("hits", 0)),
            "avg":  _safe_float(stat.get("avg")),
        }
    except (IndexError, KeyError, requests.RequestException):
        return {}


def fetch_batter_recent_ba(batter_id: int, days: int = 14) -> dict:
    """
    Batting average for a batter over the last N days from the MLB Stats API.
    Returns {ab, hits, avg}. Empty dict if no games in the window.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    url = f"{MLB_API}/people/{batter_id}/stats"
    params = {
        "stats": "byDateRange",
        "group": "hitting",
        "season": str(today.year),
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": today.strftime("%Y-%m-%d"),
        "gameType": "R",
    }
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        stat = resp.json()["stats"][0]["splits"][0]["stat"]
        return {
            "ab":   int(stat.get("atBats", 0)),
            "hits": int(stat.get("hits", 0)),
            "avg":  _safe_float(stat.get("avg")),
        }
    except (IndexError, KeyError, requests.RequestException):
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
