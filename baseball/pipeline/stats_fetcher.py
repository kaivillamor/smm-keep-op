import io
import json
import math
import os
import time
import uuid
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Real Eastern zone for wall-clock questions (day vs night game); slate dates use the
# fixed _ET_OFFSET below.
_EASTERN = ZoneInfo("America/New_York")

MLB_API = "https://statsapi.mlb.com/api/v1"
SAVANT_BASE = "https://baseballsavant.mlb.com"

# Everything keyed by slate (lineups, probable pitchers, per-day caches) uses this one
# Eastern basis, never UTC.
_ET_OFFSET = timedelta(hours=5)


def slate_date() -> str:
    """Today's MLB slate date (Eastern), as YYYY-MM-DD."""
    return (datetime.now(timezone.utc) - _ET_OFFSET).strftime("%Y-%m-%d")


# MLB postseason start, by season. Update annually.
POSTSEASON_START = {2026: "2026-09-29"}


def season_phase(game_date: str | None) -> str | None:
    """'regular' | 'postseason', or None (unknown, never a guess) when this season's
    boundary isn't recorded."""
    if not game_date:
        return None
    try:
        year = int(str(game_date)[:4])
    except (TypeError, ValueError):
        return None
    start = POSTSEASON_START.get(year)
    if not start:
        return None
    return "postseason" if str(game_date) >= start else "regular"


def game_slate_date(commence_time: str | None) -> str:
    """The MLB slate date (Eastern) a first pitch belongs to, on slate_date()'s basis: the
    key of predictions.date. Falls back to slate_date() on a missing or bad timestamp."""
    if not commence_time:
        return slate_date()
    try:
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
    except ValueError:
        return slate_date()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc) - _ET_OFFSET).strftime("%Y-%m-%d")


def is_day_game(commence_time: str | None) -> bool:
    """True if first pitch is before 6pm Eastern (DST-aware)."""
    if not commence_time:
        return False
    try:
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_EASTERN).hour < 18


def game_has_started(commence_time: str | None) -> bool:
    """True once first pitch has passed; False for an unknown or unparseable start."""
    if not commence_time:
        return False
    try:
        start = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= start

_SAVANT_TIMEOUT_CODES = {"502", "503", "524"}

# Savant's season-long CSV queries are slow: escalate the timeout across attempts.
_SAVANT_RETRY_TIMEOUTS = (25, 40)  # seconds, one per attempt
_SAVANT_RETRY_BACKOFF  = 2.0       # seconds between attempts

def _is_savant_timeout(e: Exception) -> bool:
    """True for transient Savant gateway errors (502/503/524) — not code bugs."""
    msg = str(e)
    return any(code in msg for code in _SAVANT_TIMEOUT_CODES)


def _is_transient_savant_error(e: Exception) -> bool:
    """True for anything worth retrying: read/connect timeouts, dropped
    connections, or gateway 502/503/524 — as opposed to a real code bug."""
    if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    return _is_savant_timeout(e)


# Cache root; STATS_CACHE_DIR points it at a mounted volume.
_CACHE_DIR = os.getenv("STATS_CACHE_DIR") or "data/stats"


def _day_cache_path(kind: str, player_id: int) -> str:
    """Per-player, per-slate cache path."""
    date_str = slate_date()
    return f"{_CACHE_DIR}/{kind}_{player_id}_{date_str}.json"


def _prune_old_caches(keep_days: int = 3) -> None:
    """Delete day-cache files older than keep_days. Never raises."""
    cutoff = time.time() - keep_days * 86400
    try:
        for name in os.listdir(_CACHE_DIR):
            fp = os.path.join(_CACHE_DIR, name)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass


def _read_day_cache(path: str, int_keys: bool = False) -> dict | None:
    """Returns the cached dict, or None if absent/corrupt (→ caller re-fetches)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()} if int_keys else raw
    except Exception:
        return None


def _write_day_cache(path: str, data: dict) -> None:
    """Atomic write; call only after a successful fetch. The temp name is unique per
    call, not per process: these run in a thread pool and can write the same path."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        # A failed cache write must never discard data already fetched.
        print(f"[stats_fetcher] cache write skipped ({path}): {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def _savant_csv_get(url: str, params: dict) -> str | None:
    """
    GET a Savant CSV with retries on transient errors (timeouts, gateway 5xx).
    Returns the response text, or None if every attempt hit a transient failure.
    Re-raises non-transient errors so the caller can log a genuine bug.
    """
    last_exc = None
    for attempt, timeout in enumerate(_SAVANT_RETRY_TIMEOUTS):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            if not _is_transient_savant_error(e):
                raise
            last_exc = e
            if attempt < len(_SAVANT_RETRY_TIMEOUTS) - 1:
                time.sleep(_SAVANT_RETRY_BACKOFF)
    return None  # exhausted retries on a transient error — caller returns empty


def fetch_stats() -> dict:
    _prune_old_caches()

    date_str = slate_date()
    year = date_str[:4]

    probable = _fetch_probable_pitchers(date_str)
    pitcher_ids = {
        pid
        for game in probable.values()
        for pid in [game.get("home_pitcher_id"), game.get("away_pitcher_id")]
        if pid
    }

    # MLB Stats API only; prop_pipeline fetches Savant's xERA itself.
    pitcher_stats = {str(pid): _fetch_pitcher_season_stats(pid, year) for pid in pitcher_ids}

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
    resp = requests.get(url, params=params, timeout=10)
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


def _match_probable(game_pk, probable: dict) -> dict | None:
    """One game's entry from _fetch_probable_pitchers' output, or None on a miss (never
    raises). Game IDs arrive as int from the schedule API and str from saved lineup JSON."""
    try:
        hit = probable.get(int(game_pk))
    except (TypeError, ValueError):
        hit = None
    return hit or probable.get(str(game_pk)) or None


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
    resp = requests.get(url, params=params, timeout=10)
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
            "whip": _safe_float(stat.get("whip")),
            "fip": fip,
            "bb_per_9": _safe_float(stat.get("walksPer9Inn")),
            "opp_avg": _safe_float(stat.get("avg")),
            "innings_pitched": ip,
        }
    except (IndexError, KeyError):
        return {}


def fetch_pitcher_xera(year: str) -> dict:
    """xERA (expected ERA, from Statcast contact quality) for every pitcher, as
    {player_id (str): {"xera": float}}; empty on any failure. The custom leaderboard's
    p_fip/xfip columns are empty, so this reads expected_statistics."""
    url = f"{SAVANT_BASE}/leaderboard/expected_statistics"
    params = {
        "type": "pitcher",
        "year": year,
        "position": "",
        "team": "",
        "csv": "true",
    }
    try:
        text = _savant_csv_get(url, params)
        if text is None:
            print("[stats_fetcher] fetch_pitcher_xera: Savant unavailable after retries")
            return {}
        df = pd.read_csv(io.StringIO(text))
        return {str(int(row.get("player_id", 0))): {"xera": _safe_float(row.get("xera"))}
                for _, row in df.iterrows()}
    except Exception as e:
        print(f"[stats_fetcher] fetch_pitcher_xera: {e}")
        return {}


def fetch_batter_statcast_season(year: str) -> dict:
    """
    Bulk-fetches Sweet Spot % and Hard Hit % for all batters from Baseball Savant leaderboard.
    One call for the whole season, cached daily; returns a dict keyed by player_id (str).
    """
    date_str  = slate_date()
    cache_path = f"{_CACHE_DIR}/batter_season_{date_str}.json"

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            result = json.load(f)
        print(f"[stats_fetcher] Season batter Statcast: {len(result)} batters loaded (cache)")
        return result

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
    resp = requests.get(url, params=params, timeout=30)
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

    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(result, f)

    print(f"[stats_fetcher] Season batter Statcast: {len(result)} batters loaded")
    return result


def fetch_batter_recent_stats(batter_id: int, days: int = 14) -> dict:
    """
    Hard Hit % and Sweet Spot % for a batter over the last N days, computed from raw
    batted-ball events. Empty dict if the batter had no batted balls in the window.
    """
    cache_path = _day_cache_path(f"batter_recent{days}d", batter_id)
    cached = _read_day_cache(cache_path)
    if cached is not None:
        return cached

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    url = f"{SAVANT_BASE}/statcast_search/csv"
    # statcast_search ignores start_dt/end_dt (it returns the career): filter dates with
    # game_date_gt/lt and the player with batters_lookup[].
    params = {
        "all": "true",
        "type": "batter",
        "batters_lookup[]": batter_id,
        "hfBBT": "ground_ball|line_drive|fly_ball|popup|",
        "game_date_gt": start.strftime("%Y-%m-%d"),
        "game_date_lt": today.strftime("%Y-%m-%d"),
        "hfGT": "R|",
        "sort_col": "game_date",
        "sort_order": "desc",
        "min_results": "0",
        "min_pas": "0",
    }
    try:
        text = _savant_csv_get(url, params)
        if text is None:
            return {}  # transient failure — don't cache, retry next run

        df = pd.read_csv(io.StringIO(text))
        result: dict = {}
        if not df.empty:
            result["batted_balls"] = len(df)
            if "launch_speed" in df.columns:
                ev = df["launch_speed"].dropna()
                if len(ev) > 0:
                    result["hard_hit_percent"] = float(round((ev >= 95).sum() / len(ev) * 100, 1))
            if "launch_angle" in df.columns:
                la = df["launch_angle"].dropna()
                if len(la) > 0:
                    result["sweet_spot_percent"] = float(round(((la >= 8) & (la <= 32)).sum() / len(la) * 100, 1))

        _write_day_cache(cache_path, result)
        return result
    except Exception as e:
        if not _is_transient_savant_error(e):
            print(f"[stats_fetcher] fetch_batter_recent_stats({batter_id}): {e}")
        return {}


def fetch_batter_zone_stats(batter_id: int, year: str) -> dict:
    """
    Batter xwOBA by Statcast zone for the season, from batted balls only (hfBBT, so
    estimated_woba_using_speedangle is populated): {zone_id (int): xwoba (float)}.
    """
    cache_path = _day_cache_path("batter_zone", batter_id)
    cached = _read_day_cache(cache_path, int_keys=True)
    if cached is not None:
        return cached

    today = datetime.now(timezone.utc).date()
    url = f"{SAVANT_BASE}/statcast_search/csv"
    # statcast_search ignores `player_id` under all=true (a league-wide dump) and
    # start_dt/end_dt (career data): use batters_lookup[] and game_date_gt/lt.
    params = {
        "all": "true",
        "type": "batter",
        "batters_lookup[]": batter_id,
        "game_date_gt": f"{year}-01-01",
        "game_date_lt": today.strftime("%Y-%m-%d"),
        "hfBBT": "ground_ball|line_drive|fly_ball|popup|",
        "hfGT": "R|",
        "sort_col": "game_date",
        "sort_order": "desc",
        "min_results": "0",
        "min_pas": "0",
    }
    try:
        text = _savant_csv_get(url, params)
        if text is None:
            return {}  # transient failure — don't cache, retry next run

        df = pd.read_csv(io.StringIO(text))
        result: dict = {}
        col = "estimated_woba_using_speedangle"
        if not df.empty and "zone" in df.columns and col in df.columns:
            valid = df[df["zone"].notna() & df[col].notna()].copy()
            if not valid.empty:
                valid["zone"] = valid["zone"].astype(int)
                means = valid.groupby("zone")[col].mean().round(3).to_dict()
                result = {int(z): float(v) for z, v in means.items()}

        _write_day_cache(cache_path, result)
        return result
    except Exception as e:
        if not _is_transient_savant_error(e):
            print(f"[stats_fetcher] fetch_batter_zone_stats({batter_id}): {e}")
        return {}


def fetch_pitcher_zone_tendencies(pitcher_id: int, year: str) -> dict:
    """
    A pitcher's pitch frequency by Statcast zone for the season:
    {zone_id (int): fraction_of_total_pitches (float)}.
    """
    cache_path = _day_cache_path("pitcher_zone", pitcher_id)
    cached = _read_day_cache(cache_path, int_keys=True)
    if cached is not None:
        return cached

    today = datetime.now(timezone.utc).date()
    url = f"{SAVANT_BASE}/statcast_search/csv"
    # Same quirk as the batter queries: use pitchers_lookup[] and game_date_gt/lt.
    params = {
        "all": "true",
        "type": "pitcher",
        "pitchers_lookup[]": pitcher_id,
        "game_date_gt": f"{year}-01-01",
        "game_date_lt": today.strftime("%Y-%m-%d"),
        "hfGT": "R|",
        "sort_col": "game_date",
        "sort_order": "desc",
        "min_results": "0",
        "min_pas": "0",
    }
    try:
        text = _savant_csv_get(url, params)
        if text is None:
            return {}  # transient failure — don't cache, retry next run

        df = pd.read_csv(io.StringIO(text))
        result: dict = {}
        if not df.empty and "zone" in df.columns:
            df_zones = df[df["zone"].notna()].copy()
            if not df_zones.empty:
                df_zones["zone"] = df_zones["zone"].astype(int)
                counts = df_zones["zone"].value_counts()
                total = counts.sum()
                result = {int(z): float(round(count / total, 4)) for z, count in counts.items()}

        _write_day_cache(cache_path, result)
        return result
    except Exception as e:
        if not _is_transient_savant_error(e):
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
    "Athletics":             133,
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
        resp = requests.get(url, params=params, timeout=10)
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
        resp = requests.get(url, params=params, timeout=10)
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
    cache_path = _day_cache_path(f"h2h_{pitcher_id}", batter_id)
    cached = _read_day_cache(cache_path)
    if cached is not None:
        return cached

    url = f"{MLB_API}/people/{batter_id}/stats"
    params = {
        "stats": "vsPlayer",
        "opposingPlayerId": pitcher_id,
        "group": "hitting",
        "gameType": "R",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        stat = resp.json()["stats"][0]["splits"][0]["stat"]
        result = {
            "ab":   int(stat.get("atBats", 0)),
            "hits": int(stat.get("hits", 0)),
            "avg":  _safe_float(stat.get("avg")),
        }
        _write_day_cache(cache_path, result)
        return result
    except (IndexError, KeyError):
        # No record (never faced him) is a real answer: cache it.
        _write_day_cache(cache_path, {})
        return {}
    except requests.RequestException:
        # Transient: never cache it.
        return {}


def fetch_batter_recent_ba(batter_id: int, days: int = 14) -> dict:
    """
    Batting average for a batter over the last N days from the MLB Stats API.
    Returns {ab, hits, avg}. Empty dict if no games in the window.
    """
    cache_path = _day_cache_path(f"recentba{days}d", batter_id)
    cached = _read_day_cache(cache_path)
    if cached is not None:
        return cached

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
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        stat = resp.json()["stats"][0]["splits"][0]["stat"]
        result = {
            "ab":   int(stat.get("atBats", 0)),
            "hits": int(stat.get("hits", 0)),
            "avg":  _safe_float(stat.get("avg")),
        }
        _write_day_cache(cache_path, result)
        return result
    except (IndexError, KeyError):
        # No games in the window is a real answer: cache it.
        _write_day_cache(cache_path, {})
        return {}
    except requests.RequestException:
        # Transient: never cache it.
        return {}


def fetch_batter_splits(batter_id: int, year: str = None) -> dict:
    if year is None:
        year = str(datetime.now(timezone.utc).year)

    cache_path = _day_cache_path("splits", batter_id)
    cached = _read_day_cache(cache_path)
    if cached is not None:
        return cached

    url = f"{MLB_API}/people/{batter_id}/stats"
    params = {
        "stats": "statSplits",
        "group": "hitting",
        "season": year,
        "gameType": "R",
        # REQUIRED: without sitCodes, statSplits returns an empty splits list (HTTP 200).
        "sitCodes": "vr,vl",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    splits = {"vs_rhp": {}, "vs_lhp": {}}
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            # Match on the stable code, not the description.
            split_code = split.get("split", {}).get("code", "")
            stat = split.get("stat", {})
            if split_code == "vr":
                splits["vs_rhp"] = {
                    "avg": _safe_float(stat.get("avg")),
                    "obp": _safe_float(stat.get("obp")),
                    "slg": _safe_float(stat.get("slg")),
                    "ops": _safe_float(stat.get("ops")),
                    "ab": int(stat.get("atBats", 0) or 0),
                }
            elif split_code == "vl":
                splits["vs_lhp"] = {
                    "avg": _safe_float(stat.get("avg")),
                    "obp": _safe_float(stat.get("obp")),
                    "slg": _safe_float(stat.get("slg")),
                    "ops": _safe_float(stat.get("ops")),
                    "ab": int(stat.get("atBats", 0) or 0),
                }

    _write_day_cache(cache_path, splits)
    return splits


def _safe_float(val) -> float | None:
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _save(data: dict, date_str: str) -> None:
    # The whole-slate snapshot, not the per-player cache: it does not follow _CACHE_DIR.
    path = f"data/stats/{date_str}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
