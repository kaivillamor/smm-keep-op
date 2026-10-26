"""
ESPN stats for NBA props — scoreboard, rosters, player game logs, box scores.

ESPN is the primary source and needs no key. PROJECT.md §23 verified the alternatives and
ruled them out: `stats.nba.com` READ TIMEOUTs even with full browser headers and would be
worse from Railway's datacenter IPs (the popular `nba_api` package wraps it and inherits
the failure), and balldontlie.io now 401s without a key. Do not reintroduce either.

Endpoint shapes verified live 2026-10-20:

    scoreboard  site.api.espn.com/.../scoreboard?dates=YYYYMMDD
                -> events[].{id, date, competitions[0].competitors[].{homeAway, team}}
    roster      site.api.espn.com/.../teams/{abbrev}/roster -> athletes[].{id, displayName}
    gamelog     site.web.api.espn.com/.../athletes/{id}/gamelog
                -> names[]               positional stat labels
                   seasonTypes[].categories[].events[].{eventId, stats[]}
                   events{eventId: {gameDate, score, homeTeamId, atVs}}
                The stats live in seasonTypes; the DATES live in the top-level events map.
                Joining the two is the only way to get a dated game log.
    boxscore    site.api.espn.com/.../summary?event={id}
                -> boxscore.players[].statistics[].athletes[].{athlete, stats[]}

Every stat arrives as a STRING, and the combined ones as "made-attempted" ("2-3").
"""
import json
import os
import time
from datetime import datetime, timezone

import requests

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
WEB_API = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"

_TIMEOUT = 20
_CACHE_DIR = os.getenv("NBA_CACHE_DIR") or "data/stats"

# Positional labels from the gamelog `names` array, verified 2026-10-20. Index into the
# per-game `stats` list. Named here rather than read from the response because a silent
# reordering upstream should break loudly on a key error, not shift every stat by one.
STAT_INDEX = {
    "minutes": 0, "fg": 1, "fg_pct": 2, "threes": 3, "three_pct": 4,
    "ft": 5, "ft_pct": 6, "rebounds": 7, "assists": 8, "blocks": 9,
    "steals": 10, "fouls": 11, "turnovers": 12, "points": 13,
}

# ESPN states the season type on every event, so the phase is READ, never inferred from a
# date. A hardcoded boundary got this wrong on day one: PROJECT.md §23 estimated the 2026-27
# regular season would start 2026-10-21, the opener was actually 2026-10-20, and 134 real
# regular-season rows were logged as preseason. An authoritative field needs no annual
# maintenance and cannot drift.
ESPN_SEASON_TYPE = {1: "preseason", 2: "regular", 3: "postseason", 4: "allstar"}

# The prop market -> the stat it resolves against. Adding a market without adding its
# mapping here would silently grade every row of it as a loss.
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


def _get(url: str, params: dict | None = None, *, retries: int = 3) -> dict | None:
    """GET with backoff. Returns None rather than raising: one unreachable player must
    not take down a whole slate's collection."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except (requests.RequestException, json.JSONDecodeError):
            pass
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
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
    """Games for an Eastern slate date (YYYY-MM-DD).

    Returns {game_id, tipoff (ISO-8601 UTC), home, away, status, final}. `tipoff` is what
    keys every prediction row — see pipeline/calendar.game_slate_date.
    """
    compact = day.replace("-", "")
    # NEVER cached. Rosters and game logs are stable within a day; the scoreboard is the
    # one response whose meaning changes during it — game status goes pre -> in -> post.
    # It used to be cached per day like the rest, so the evening collect run stored every
    # game as 'pre', and the next morning's grade run read that file, saw no final games,
    # and skipped the whole slate. Six days, 2,533 rows, zero graded (2026-10-20..26).
    # One call per run is cheap; a stale status is not.
    data = _get(f"{SITE_API}/scoreboard", {"dates": compact})
    if data is None:
        print(f"[espn] scoreboard unavailable for {day}")
        return []

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
            "status": status.get("name"),
            # 'pre' | 'in' | 'post'. `completed` alone is NOT enough to decide whether to
            # collect: it is False for an IN-PROGRESS game as well as a scheduled one, so
            # a `not final` filter re-scores live games and overwrites their pre-tip price
            # with an in-play one. A prop you can no longer bet is not the price the model
            # should be measured against. MLB guards this with game_has_started().
            "state": status.get("state"),
            "started": status.get("state") != "pre",
            # None for an unrecognised type rather than a guess — the same rule the date
            # fallback follows, for the same reason.
            "phase": ESPN_SEASON_TYPE.get((ev.get("season") or {}).get("type")),
            "season_year": (ev.get("season") or {}).get("year"),
            # Only FINAL games may be graded. Box scores populate live, and baseball
            # graded a player a loss before his game started by trusting a non-final one.
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
            out.append({"player_id": str(a["id"]), "name": a["displayName"], "team": team})
    return out


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def strip_suffix(normalized_name: str) -> str:
    """'jabari smith jr' -> 'jabari smith'. Expects an already-normalized name."""
    parts = normalized_name.split()
    if len(parts) > 2 and parts[-1] in _SUFFIXES:
        return " ".join(parts[:-1])
    return normalized_name


def build_player_index(games: list[dict], day: str, normalize) -> dict:
    """{normalized name: {player_id, team, opponent}} for everyone playing this slate.

    The prop feed identifies players by NAME only — it carries no player id (§ the note in
    prop_odds) — so a name index is the join between the odds board and ESPN.
    """
    index: dict[str, dict] = {}
    for g in games:
        for side, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            if not side:
                continue
            for p in fetch_roster(side, day):
                row = {
                    "player_id": p["player_id"], "team": side, "opponent": opp,
                    "game_id": g["game_id"], "tipoff": g["tipoff"],
                    "is_home": 1 if side == g["home"] else 0,
                }
                key = normalize(p["name"])
                index[key] = row
                # Also index without a generational suffix. The board writes "Jabari
                # Smith" where ESPN has "Jabari Smith Jr." — measured on the 2026-10-21
                # board, that one mismatch was the ONLY genuine miss out of 108 players.
                # The alias never overwrites a real name, so a "Jr." who shares a stripped
                # key with his father still resolves to whoever is actually on the roster.
                bare = strip_suffix(key)
                if bare != key:
                    index.setdefault(bare, row)
    return index


# ── player form ──────────────────────────────────────────────────────────────

def fetch_gamelog(player_id: str, day: str) -> dict:
    """Dated game log, newest first, grouped by season type.

    Returns {season_label: [{event_id, date, stats{...}}, ...]}. Joins the stat arrays in
    `seasonTypes` with the dates in the top-level `events` map — ESPN splits them, and the
    stats alone carry no date, so neither half is usable on its own.
    """
    cached = _read_cache(_cache_path("gamelog", player_id, day))
    data = cached or _get(f"{WEB_API}/athletes/{player_id}/gamelog")
    if data is None:
        return {}
    if cached is None:
        _write_cache(_cache_path("gamelog", player_id, day), data)

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
    """The best available REGULAR-season log, newest season first, and its label.

    Preseason is never used: starters play roughly 15 minutes, and minutes drive every
    prop in this model, so preseason form is a different sport (see calendar.season_phase).
    On opening night the current season has no games, so this falls back to last season —
    which is why the chosen label is returned and logged. A season average silently
    switching source mid-corpus is exactly the unlogged-method problem §24 exists for.
    """
    regular = {k: v for k, v in gamelog.items()
               if "Regular Season" in k and "Play In" not in k and v}
    if not regular:
        return None, []
    labels = sorted(regular, reverse=True)
    current = labels[0]
    if len(labels) == 1:
        return current, [dict(g, prior=False) for g in regular[current]]

    # BLEND this season with last season rather than switching wholesale. Taking the newest
    # season with any games meant that after a player's first game, their 70-game log was
    # dropped for a 1-game one: in week one, 64% of graded rows (1,310) were predicted off
    # 1-3 games and ranked at AUC 0.512, against 0.536 for rows still on last season's log.
    # Last season's games ride along tagged `prior`; player_form shrinks toward them.
    previous = labels[1]
    rows = ([dict(g, prior=False) for g in regular[current]] +
            [dict(g, prior=True) for g in regular[previous]])
    n_cur = len(regular[current])
    # The label carries the method, because what `season_avg` means changed on 2026-10-26:
    # before, one season's raw mean; after, a shrunk blend. §24 — a column means one method
    # forever, so rows have to be able to say which one produced them.
    return f"{current} ({n_cur}g) + prior {previous}", rows


PRIOR_GAMES = 15


def player_form(games: list[dict], market: str, recent_n: int = 10) -> dict:
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

    # ESPN carries no true usage rate (§23), so approximate it. Recent games only — usage
    # moves with role changes, and a season-long figure would lag a rotation change.
    usage = [g["fga"] + 0.44 * g["fta"] + g["turnovers"]
             for g in games[:recent_n]
             if g.get("fga") is not None and g.get("fta") is not None
             and g.get("turnovers") is not None]

    avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None

    # Sample SD of this player's own log. The model needs a spread to turn an average
    # into P(value > line), and a per-player empirical SD beats any constant guessed per
    # market — a 30-minute starter and a bench scorer with the same mean have very
    # different distributions.
    sd = None
    if len(vals) >= 5:
        m = sum(vals) / len(vals)
        sd = round((sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5, 3)

    # Shrink this season toward last season by games played: last season counts as
    # PRIOR_GAMES games' worth of evidence. One game this season moves the average about
    # 6%; by 15 games it carries half the weight; by 45, three quarters. Not fitted — it
    # is a sensible prior to be replaced once the corpus can fit one.
    if cur and prev:
        prior_mean = sum(prev) / len(prev)
        season = round((sum(cur) + PRIOR_GAMES * prior_mean) / (len(cur) + PRIOR_GAMES), 3)
    else:
        season = avg(cur or prev)

    return {
        "stat_sd": sd,
        "season_avg": season,
        "recent_avg": avg(recent),
        "recent_games": len(recent),
        # Recent minutes, not season: a player whose role just changed is the single most
        # common way an NBA prop model goes wrong.
        "minutes_avg": avg(rec_mins) if rec_mins else avg(mins),
        "usage_proxy": avg(usage),
    }


def days_rest(games: list[dict], tipoff: str | None) -> int | None:
    """Days since this player's last game. Back-to-backs matter far more than in MLB."""
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


# ── grading ──────────────────────────────────────────────────────────────────

def fetch_boxscore(game_id: str) -> dict:
    """{player_id: {points, rebounds, assists, threes_made, minutes}} for a FINAL game.

    ONE call per game, not per player — baseball proved the per-player version turns a
    180-row slate into 180 requests. Not cached: a box score is fetched once, after the
    game, and caching it would only risk serving a partial one.
    """
    data = _get(f"{SITE_API}/summary", {"event": game_id})
    if data is None:
        return {}
    out: dict[str, dict] = {}
    for team in (data.get("boxscore") or {}).get("players", []):
        for group in team.get("statistics", []):
            keys = [str(k).lower() for k in (group.get("keys") or [])]
            labels = [str(l).lower() for l in (group.get("labels") or [])]
            for a in group.get("athletes", []):
                pid = str(((a.get("athlete") or {}).get("id")) or "")
                raw = a.get("stats") or []
                if not pid or not raw:
                    continue
                # Index by both namings — `keys` are camelCase full names
                # ('rebounds', 'threePointFieldGoalsMade-...'), `labels` the short
                # column heads ('REB', '3PT'). Either alone is one upstream rename
                # away from silently returning None for a stat we grade on.
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
                }
    return out
