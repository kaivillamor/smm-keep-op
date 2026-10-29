"""
ESPN stats for NBA props (no key): scoreboard, rosters, player game logs, box scores.

    scoreboard  site.api.espn.com/.../scoreboard?dates=YYYYMMDD
                -> events[].{id, date, competitions[0].competitors[].{homeAway, team}}
    roster      site.api.espn.com/.../teams/{abbrev}/roster -> athletes[].{id, displayName}
    gamelog     site.web.api.espn.com/.../athletes/{id}/gamelog
                -> names[]               positional stat labels
                   seasonTypes[].categories[].events[].{eventId, stats[]}
                   events{eventId: {gameDate, score, homeTeamId, atVs}}
                Stats live in seasonTypes and dates in the top-level events map: join them.
    boxscore    site.api.espn.com/.../summary?event={id}
                -> boxscore.players[].statistics[].athletes[].{athlete, stats[]}

Every stat arrives as a STRING, and the combined ones as "made-attempted" ("2-3").
"""
import json
import os
import re
import shutil
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
WEB_API = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"

_TIMEOUT = 20
_CACHE_DIR = os.getenv("NBA_CACHE_DIR") or "data/stats"

# Index into each game's `stats` list, per the gamelog's `names` array.
STAT_INDEX = {
    "minutes": 0, "fg": 1, "fg_pct": 2, "threes": 3, "three_pct": 4,
    "ft": 5, "ft_pct": 6, "rebounds": 7, "assists": 8, "blocks": 9,
    "steals": 10, "fouls": 11, "turnovers": 12, "points": 13,
}

ESPN_SEASON_TYPE = {1: "preseason", 2: "regular", 3: "postseason", 4: "allstar"}

# Prop market -> the box-score stat it grades against; a new market needs its entry here.
MARKET_STAT = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_made_threes": "threes_made",
}


# ── plumbing ─────────────────────────────────────────────────────────────────

def _cache_path(kind: str, key: str, day: str) -> str:
    return os.path.join(_CACHE_DIR, day, f"{kind}_{key}.json")


def _read_cache(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: str, payload) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass          # a cache miss is slower, never wrong


CACHE_KEEP_DAYS = 2           # slate folders kept: tonight's and last night's
_DAY_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")
_SEASON_FILE = re.compile(r"gamelog_\d+_(\d{4})\.json")


def prune_cache(day: str, oldest_season: int | None = None) -> int:
    """Delete slate folders older than CACHE_KEEP_DAYS and season logs before
    `oldest_season`; returns how many went. Never raises."""
    cutoff = (date.fromisoformat(day) - timedelta(days=CACHE_KEEP_DAYS - 1)).isoformat()
    removed = 0
    try:
        for name in os.listdir(_CACHE_DIR):
            path = os.path.join(_CACHE_DIR, name)
            if _DAY_DIR.fullmatch(name) and name < cutoff and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed += not os.path.exists(path)
        seasons = os.path.join(_CACHE_DIR, "seasons")
        if oldest_season and os.path.isdir(seasons):
            for name in os.listdir(seasons):
                m = _SEASON_FILE.fullmatch(name)
                if m and int(m.group(1)) < oldest_season:
                    try:
                        os.remove(os.path.join(seasons, name))
                        removed += 1
                    except OSError:
                        pass
    except OSError:
        pass
    return removed


_MIN_INTERVAL = 0.1           # seconds between ESPN requests
_last_call = 0.0

# Requests that exhausted their retries this process, by endpoint kind.
FETCH_FAILURES: Counter = Counter()


def _get(url: str, params: dict | None = None, *, retries: int = 4) -> dict | None:
    """GET with pacing and backoff (honours Retry-After; a 404 is final). Returns None
    rather than raising."""
    global _last_call
    for attempt in range(retries):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        retry_after = 0.0
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            retry_after = _num(r.headers.get("Retry-After")) or 0.0
        except (requests.RequestException, json.JSONDecodeError):
            pass
        if attempt < retries - 1:
            time.sleep(max(retry_after, 2.0 * (attempt + 1)))
    return None


def _num(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _made_att(raw) -> tuple[float | None, float | None]:
    """'7-13' -> (7.0, 13.0). The combined stats arrive only in this form."""
    if not isinstance(raw, str) or "-" not in raw:
        return None, None
    made, _, att = raw.partition("-")
    return _num(made), _num(att)


# ── schedule ─────────────────────────────────────────────────────────────────

def fetch_scoreboard(day: str) -> list[dict]:
    """Games for an Eastern slate date (YYYY-MM-DD): {game_id, tipoff (ISO-8601 UTC), home,
    away, status, final, ...}."""
    data = scoreboard_data(day)
    if data is None:
        print(f"[espn] scoreboard unavailable for {day}")
        return []
    return parse_scoreboard(data)


def scoreboard_data(day: str) -> dict | None:
    """Raw scoreboard JSON, or None if the fetch failed (fetch_scoreboard returns [] for
    both "no games" and a failure)."""
    compact = day.replace("-", "")
    # Never cached: game status changes during the day (pre -> in -> post).
    return _get(f"{SITE_API}/scoreboard", {"dates": compact})


def _team_box(competitor: dict) -> dict | None:
    """A team's own box totals for one game, or None (never a dict of Nones) if it has no
    box: a postponed game shows state 'post' with no statistics."""
    st = {x.get("name"): x.get("displayValue") for x in competitor.get("statistics", [])}
    if _num(st.get("points")) is None:
        return None
    return {"points": _num(st.get("points")), "rebounds": _num(st.get("rebounds")),
            "assists": _num(st.get("assists")),
            "threes": _num(st.get("threePointFieldGoalsMade"))}


def parse_scoreboard(data: dict) -> list[dict]:
    games = []
    for ev in data.get("events", []):
        comps = ev.get("competitions") or [{}]
        comp = comps[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        status = ((comp.get("status") or {}).get("type") or {})
        games.append({
            "game_id": ev.get("id"),
            "tipoff": ev.get("date"),
            "home": ((sides.get("home") or {}).get("team") or {}).get("abbreviation"),
            "away": ((sides.get("away") or {}).get("team") or {}).get("abbreviation"),
            # Team ids, not abbreviations, are the join key across ESPN endpoints.
            "home_id": str(((sides.get("home") or {}).get("team") or {}).get("id") or ""),
            "away_id": str(((sides.get("away") or {}).get("team") or {}).get("id") or ""),
            "home_box": _team_box(sides["home"]) if status.get("state") == "post"
                        and sides.get("home") else None,
            "away_box": _team_box(sides["away"]) if status.get("state") == "post"
                        and sides.get("away") else None,
            "status": status.get("name"),
            # 'pre' | 'in' | 'post'. Collect on `started`, not `final`: `completed` is
            # also False mid-game.
            "state": status.get("state"),
            "started": status.get("state") != "pre",
            "phase": ESPN_SEASON_TYPE.get((ev.get("season") or {}).get("type")),
            "season_year": (ev.get("season") or {}).get("year"),
            # 'STD' for an ordinary game; ESPN tags All-Star games as regular season.
            "comp_type": (comp.get("type") or {}).get("abbreviation"),
            # Only final games may be graded: box scores populate live.
            "final": bool(status.get("completed")),
        })
    return games


def fetch_roster(team: str, day: str) -> list[dict]:
    """Active roster for a team abbreviation, cached per day."""
    cached = _read_cache(_cache_path("roster", team, day))
    data = cached or _get(f"{SITE_API}/teams/{team}/roster")
    if data is None:
        return []
    if cached is None:
        _write_cache(_cache_path("roster", team, day), data)
    out = []
    for a in data.get("athletes", []):
        if a.get("id") and a.get("displayName"):
            pos = (a.get("position") or {}).get("abbreviation")
            out.append({"player_id": str(a["id"]), "name": a["displayName"], "team": team,
                        "position": position_group(pos)})
    return out


# ESPN positions folded into G/F/C; anything else is None.
_POSITION_GROUP = {"PG": "G", "SG": "G", "G": "G", "SF": "F", "PF": "F", "F": "F", "C": "C"}


def position_group(abbrev) -> str | None:
    return _POSITION_GROUP.get(str(abbrev or "").upper())


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def strip_suffix(normalized_name: str) -> str:
    """'jabari smith jr' -> 'jabari smith'. Expects an already-normalized name."""
    parts = normalized_name.split()
    if len(parts) > 2 and parts[-1] in _SUFFIXES:
        return " ".join(parts[:-1])
    return normalized_name


def build_player_index(games: list[dict], day: str, normalize) -> dict:
    """{normalized name: {player_id, team, opponent, ...}} for everyone playing this slate.
    The prop feed carries names, not ids, so this is its join to ESPN."""
    index: dict[str, dict] = {}
    for g in games:
        for side, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            if not side:
                continue
            for p in fetch_roster(side, day):
                home = side == g["home"]
                row = {
                    "player_id": p["player_id"], "team": side, "opponent": opp,
                    "team_id": g["home_id"] if home else g["away_id"],
                    "opponent_id": g["away_id"] if home else g["home_id"],
                    "game_id": g["game_id"], "tipoff": g["tipoff"],
                    "is_home": 1 if home else 0, "position": p.get("position"),
                }
                key = normalize(p["name"])
                index[key] = row
                # Also index without a generational suffix; never over a real name.
                bare = strip_suffix(key)
                if bare != key:
                    index.setdefault(bare, row)
    return index


# ── player form ──────────────────────────────────────────────────────────────

def fetch_gamelog(player_id: str, day: str, prior_year: int | None = None) -> dict:
    """Dated game log, newest first, grouped by season type: this season and last.

    Returns {season_label: [{event_id, date, stats{...}}, ...]}. The endpoint's default is
    only the latest season with games, so last season is asked for by year (ESPN labels a
    season by the year it ends) and cached permanently.
    """
    current = _gamelog_data(player_id, None, _cache_path("gamelog", player_id, day))
    out = _parse_gamelog(current) if current else {}
    if prior_year:
        prior = _gamelog_data(player_id, prior_year,
                              _cache_path("gamelog", f"{player_id}_{prior_year}", "seasons"))
        if prior is None:
            FETCH_FAILURES["prior_gamelog"] += 1
        if prior:
            for label, rows in _parse_gamelog(prior).items():
                out.setdefault(label, rows)
    return out


def fetch_season_log(player_id: str, season_year: int) -> list[dict]:
    """One finished season's regular-season games, newest first; [] if unavailable. For
    the method's head-to-head only: kept out of fetch_gamelog so pick_season never sees it.
    """
    data = _gamelog_data(player_id, season_year,
                         _cache_path("gamelog", f"{player_id}_{season_year}", "seasons"))
    if data is None:
        FETCH_FAILURES["older_gamelog"] += 1
        return []
    return [g for label, rows in _parse_gamelog(data).items()
            if "Regular Season" in label and "Play In" not in label for g in rows]


def _gamelog_data(player_id: str, season: int | None, cache: str) -> dict | None:
    cached = _read_cache(cache)
    if cached is not None:
        return cached
    data = _get(f"{WEB_API}/athletes/{player_id}/gamelog",
                {"season": season} if season else None)
    if data is not None:
        _write_cache(cache, data)
    return data


def _parse_gamelog(data: dict) -> dict:
    """Join the stat arrays in `seasonTypes` with the dates in the top-level `events` map."""
    meta = data.get("events") or {}
    out: dict[str, list] = {}
    for st in data.get("seasonTypes", []):
        label = st.get("displayName") or "?"
        rows = []
        for cat in st.get("categories", []):
            for ev in cat.get("events", []):
                raw = ev.get("stats") or []
                if len(raw) <= STAT_INDEX["points"]:
                    continue
                fgm, fga = _made_att(raw[STAT_INDEX["fg"]])
                ftm, fta = _made_att(raw[STAT_INDEX["ft"]])
                tpm, tpa = _made_att(raw[STAT_INDEX["threes"]])
                m = meta.get(ev.get("eventId"), {})
                rows.append({
                    "event_id": ev.get("eventId"),
                    "date": m.get("gameDate"),
                    "opponent_id": str((m.get("opponent") or {}).get("id") or ""),
                    "minutes": _num(raw[STAT_INDEX["minutes"]]),
                    "points": _num(raw[STAT_INDEX["points"]]),
                    "rebounds": _num(raw[STAT_INDEX["rebounds"]]),
                    "assists": _num(raw[STAT_INDEX["assists"]]),
                    "turnovers": _num(raw[STAT_INDEX["turnovers"]]),
                    "threes_made": tpm, "threes_att": tpa,
                    "fga": fga, "fta": fta, "fgm": fgm, "ftm": ftm,
                })
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
        out[label] = rows
    return out


def pick_season(gamelog: dict) -> tuple[str | None, list]:
    """(label, rows) of regular-season games, never preseason: this season plus last
    season's games tagged `prior`, or one season when only one has games."""
    regular = {k: v for k, v in gamelog.items()
               if "Regular Season" in k and "Play In" not in k and v}
    if not regular:
        return None, []
    labels = sorted(regular, reverse=True)
    current = labels[0]
    if len(labels) == 1:
        return current, [dict(g, prior=False) for g in regular[current]]

    previous = labels[1]
    rows = ([dict(g, prior=False) for g in regular[current]] +
            [dict(g, prior=True) for g in regular[previous]])
    n_cur = len(regular[current])
    return f"{current} ({n_cur}g) + prior {previous}", rows


PRIOR_GAMES = 15


def player_form(games: list[dict], market: str, recent_n: int = 10,
                opponent_id: str | None = None) -> dict:
    """Season / recent / minutes / usage inputs for one player and market."""
    stat = MARKET_STAT.get(market)
    if not stat or not games:
        return {}
    cur = [g[stat] for g in games if not g.get("prior") and g.get(stat) is not None]
    prev = [g[stat] for g in games if g.get("prior") and g.get(stat) is not None]
    vals = [g[stat] for g in games if g.get(stat) is not None]
    mins = [g["minutes"] for g in games if g.get("minutes") is not None]
    recent = [g[stat] for g in games[:recent_n] if g.get(stat) is not None]
    rec_mins = [g["minutes"] for g in games[:recent_n] if g.get("minutes") is not None]

    # Usage proxy (ESPN has no usage rate), recent games only.
    usage = [g["fga"] + 0.44 * g["fta"] + g["turnovers"]
             for g in games[:recent_n]
             if g.get("fga") is not None and g.get("fta") is not None
             and g.get("turnovers") is not None]

    avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None

    # Sample SD of this player's own log.
    sd = None
    if len(vals) >= 5:
        m = sum(vals) / len(vals)
        sd = round((sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5, 3)

    # Shrink this season toward last season, which counts as PRIOR_GAMES games.
    if cur and prev:
        prior_mean = sum(prev) / len(prev)
        season = round((sum(cur) + PRIOR_GAMES * prior_mean) / (len(cur) + PRIOR_GAMES), 3)
    else:
        season = avg(cur or prev)

    # This market against tonight's opponent, across both seasons.
    vs = [g[stat] for g in games
          if opponent_id and g.get("opponent_id") == opponent_id and g.get(stat) is not None]

    return {
        "vs_opp_avg": avg(vs),
        "vs_opp_games": len(vs),
        "stat_sd": sd,
        "season_avg": season,
        "recent_avg": avg(recent),
        "recent_games": len(recent),
        "minutes_avg": avg(rec_mins) if rec_mins else avg(mins),
        "usage_proxy": avg(usage),
    }


def days_rest(games: list[dict], tipoff: str | None) -> int | None:
    """Days since this player's last game."""
    if not games or not tipoff:
        return None
    last = next((g["date"] for g in games if g.get("date")), None)
    if not last:
        return None
    try:
        a = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(tipoff).replace("Z", "+00:00"))
    except ValueError:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return max((b.date() - a.date()).days, 0)


# ── availability ─────────────────────────────────────────────────────────────

_ID_IN_LINK = re.compile(r"/id/(\d+)")


def fetch_injuries() -> dict | None:
    """League injury report: {player_id: {status, team_id, name}}, or None if it could not
    be fetched (not the same as nobody hurt). Not cached. Entries have no id field, so the
    id is read from the profile link ('/nba/player/_/id/4712863/...')."""
    data = _get(f"{SITE_API}/injuries")
    if data is None:
        return None
    out: dict[str, dict] = {}
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            a = inj.get("athlete") or {}
            pid = next((m.group(1) for link in (a.get("links") or [])
                        if (m := _ID_IN_LINK.search(link.get("href") or ""))), None)
            if not pid:
                continue
            out[pid] = {
                "status": (inj.get("status") or "").strip().lower() or None,
                "team_id": str((a.get("team") or {}).get("id") or team.get("id") or ""),
                "name": a.get("displayName"),
            }
    return out


def recent_current_minutes(games: list[dict], season_year: int, n: int = 5) -> float:
    """Average minutes over a player's last n games this season, filtered by game date
    (not pick_season's label); 0.0 if none."""
    start = f"{season_year - 1}-09-01"      # ESPN labels a season by the year it ends
    cur = [g["minutes"] for g in games
           if (g.get("date") or "") >= start and g.get("minutes") is not None][:n]
    return round(sum(cur) / len(cur), 1) if cur else 0.0


def recent_current_usage(games: list[dict], season_year: int, n: int = 5) -> float:
    """FGA + 0.44*FTA + TOV per game over a player's last n games this season, filtered by
    game date; 0.0 if none."""
    start = f"{season_year - 1}-09-01"
    cur = [g["fga"] + 0.44 * g["fta"] + g["turnovers"] for g in games
           if (g.get("date") or "") >= start and g.get("fga") is not None
           and g.get("fta") is not None and g.get("turnovers") is not None][:n]
    return round(sum(cur) / len(cur), 1) if cur else 0.0


# ── grading ──────────────────────────────────────────────────────────────────

def fetch_boxscore(game_id: str) -> dict:
    """{player_id: {points, rebounds, assists, threes_made, minutes, team_id, position}} for
    a FINAL game. One call per game, never cached, so a partial box can't stick."""
    data = _get(f"{SITE_API}/summary", {"event": game_id})
    if data is None:
        return {}
    out: dict[str, dict] = {}
    for team in (data.get("boxscore") or {}).get("players", []):
        team_id = str((team.get("team") or {}).get("id") or "")
        for group in team.get("statistics", []):
            keys = [str(k).lower() for k in (group.get("keys") or [])]
            labels = [str(l).lower() for l in (group.get("labels") or [])]
            for a in group.get("athletes", []):
                pid = str(((a.get("athlete") or {}).get("id")) or "")
                raw = a.get("stats") or []
                if not pid or not raw:
                    continue
                # Index by both `keys` (full names) and `labels` (short heads), so one
                # rename upstream can't blank a graded stat.
                row = {}
                if len(raw) == len(keys):
                    row.update(dict(zip(keys, raw)))
                if len(raw) == len(labels):
                    row.update(dict(zip(labels, raw)))
                if not row:
                    continue
                pick = lambda *names: next(
                    (row[nm] for nm in names if row.get(nm) not in (None, "")), None)
                tpm, _ = _made_att(pick(
                    "threepointfieldgoalsmade-threepointfieldgoalsattempted", "3pt"))
                out[pid] = {
                    "minutes": _num(pick("minutes", "min")),
                    "points": _num(pick("points", "pts")),
                    "rebounds": _num(pick("rebounds", "totalrebounds", "reb")),
                    "assists": _num(pick("assists", "ast")),
                    "threes_made": tpm,
                    "team_id": team_id,
                    "position": position_group(
                        (((a.get("athlete") or {}).get("position")) or {}).get("abbreviation")),
                }
    return out
