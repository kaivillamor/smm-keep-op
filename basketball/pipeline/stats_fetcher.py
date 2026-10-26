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
import re
import time
from collections import Counter
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


# Pacing between ESPN requests. A collect run is a burst of ~250 game-log requests (this
# season and last, per player) plus rosters, and on 2026-10-26 roughly half the last-season
# requests in one run failed while the same URLs succeeded moments later — throttling. A
# tenth of a second each adds ~25s to a run that already takes minutes.
_MIN_INTERVAL = 0.1
_last_call = 0.0

# Requests that exhausted their retries this process, by endpoint kind. Reported by the
# collector so a degraded run says so instead of quietly logging worse inputs.
FETCH_FAILURES: Counter = Counter()


def _get(url: str, params: dict | None = None, *, retries: int = 4) -> dict | None:
    """GET with pacing and backoff. Returns None rather than raising: one unreachable
    player must not take down a whole slate's collection.

    Throttling (429) and server errors back off and honour Retry-After; a 404 is final.
    """
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
    """Games for an Eastern slate date (YYYY-MM-DD).

    Returns {game_id, tipoff (ISO-8601 UTC), home, away, status, final}. `tipoff` is what
    keys every prediction row — see pipeline/calendar.game_slate_date.
    """
    data = scoreboard_data(day)
    if data is None:
        print(f"[espn] scoreboard unavailable for {day}")
        return []
    return parse_scoreboard(data)


def scoreboard_data(day: str) -> dict | None:
    """Raw scoreboard JSON, or None if the fetch FAILED.

    Kept separate from fetch_scoreboard because that returns [] for both "no games" and
    "request failed" — fine for collection, fatal for the team-defense backfill, which
    marks a date as done and would then skip a failed date forever.
    """
    compact = day.replace("-", "")
    # NEVER cached. Rosters and game logs are stable within a day; the scoreboard is the
    # one response whose meaning changes during it — game status goes pre -> in -> post.
    # It used to be cached per day like the rest, so the evening collect run stored every
    # game as 'pre', and the next morning's grade run read that file, saw no final games,
    # and skipped the whole slate. Six days, 2,533 rows, zero graded (2026-10-20..26).
    # One call per run is cheap; a stale status is not.
    return _get(f"{SITE_API}/scoreboard", {"dates": compact})


def _team_box(competitor: dict) -> dict | None:
    """A team's own box totals for one game, or None if the game has no box.

    None rather than a dict of Nones: a postponed game reports state 'post' with no
    statistics at all, and a dict of empty values is still truthy — four postponed
    2025-26 games were stored as played until this returned None.
    """
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
            # Team IDS are the join key everywhere a team is matched across endpoints. The
            # scoreboard writes "NY", "SA", "WSH"; other ESPN endpoints are not guaranteed
            # to agree, and an abbreviation mismatch would leave a column silently empty.
            "home_id": str(((sides.get("home") or {}).get("team") or {}).get("id") or ""),
            "away_id": str(((sides.get("away") or {}).get("team") or {}).get("id") or ""),
            # Team box totals, final games only — the raw material for opponent defense.
            "home_box": _team_box(sides["home"]) if status.get("state") == "post"
                        and sides.get("home") else None,
            "away_box": _team_box(sides["away"]) if status.get("state") == "post"
                        and sides.get("away") else None,
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
            # 'STD' for an ordinary game. ESPN's SEASON type cannot be trusted for this:
            # the 2026 All-Star round robin (teams STARS, STRIPES, WORLD) is tagged season
            # type 2, regular season, and only the competition type says 'ALLSTAR'.
            "comp_type": (comp.get("type") or {}).get("abbreviation"),
            # Only FINAL games may be graded. Box scores populate live, and baseball
            # graded a player a loss before their game started by trusting a non-final one.
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
                home = side == g["home"]
                row = {
                    "player_id": p["player_id"], "team": side, "opponent": opp,
                    "team_id": g["home_id"] if home else g["away_id"],
                    "opponent_id": g["away_id"] if home else g["home_id"],
                    "game_id": g["game_id"], "tipoff": g["tipoff"],
                    "is_home": 1 if home else 0,
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

def fetch_gamelog(player_id: str, day: str, prior_year: int | None = None) -> dict:
    """Dated game log, newest first, grouped by season type — this season AND last.

    Returns {season_label: [{event_id, date, stats{...}}, ...]}.

    Two requests, because the endpoint's default is only the LATEST season with games. On
    opening night that happened to be 2025-26, so both seasons appeared to be there; once
    a player had played this season it returned 2026-27 alone — two games for a starter —
    and the blend in pick_season silently had no prior to blend with. Last season has to
    be asked for by year (`?season=2026` for 2025-26; ESPN labels seasons by the year
    they end). A finished season never changes, so that half is cached permanently; the
    current half is cached per day like everything else that is stable within one.
    """
    current = _gamelog_data(player_id, None, _cache_path("gamelog", player_id, day))
    out = _parse_gamelog(current) if current else {}
    if prior_year:
        prior = _gamelog_data(player_id, prior_year,
                              _cache_path("gamelog", f"{player_id}_{prior_year}", "seasons"))
        if prior is None:
            # Not cached on failure, so the next hourly run retries it. Until then this
            # player is predicted from this season alone — counted so the run reports it.
            FETCH_FAILURES["prior_gamelog"] += 1
        if prior:
            for label, rows in _parse_gamelog(prior).items():
                out.setdefault(label, rows)
    return out


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
    """Join the stat arrays in `seasonTypes` with the dates in the top-level `events` map —
    ESPN splits them, and the stats alone carry no date, so neither half is usable alone."""
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

    # This market against tonight's opponent, across both seasons. LOGGED, not used: two
    # to eight games is a noisy sample, and MLB found its streak-style inputs carried no
    # signal at all (§29). It earns a place in the model only if the corpus says so.
    vs = [g[stat] for g in games
          if opponent_id and g.get("opponent_id") == opponent_id and g.get(stat) is not None]

    return {
        "vs_opp_avg": avg(vs),
        "vs_opp_games": len(vs),
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


# ── availability ─────────────────────────────────────────────────────────────

_ID_IN_LINK = re.compile(r"/id/(\d+)")


def fetch_injuries() -> dict | None:
    """League injury report: {player_id: {status, team_id, name}}.

    Returns None if the report could not be fetched, which is NOT the same as an empty
    report. Treating a failed fetch as "nobody is hurt" would log every player healthy on
    exactly the nights the data is missing — a fallback masquerading as data. Not cached:
    the report changes through the day, and the latest one before tip is the useful one.

    The injury entries carry no player id field; the id is read from the profile link
    ('/nba/player/_/id/4712863/...'), which is the same id the rosters and logs use.
    """
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
    """Average minutes over a player's last n games THIS season; 0.0 if none.

    Filtered by game DATE, not by pick_season's label. A player out since preseason has no
    current-season games, so pick_season falls back to last season and those rows look
    "current" — counting their minutes as missing tonight would double-count an absence
    every teammate's recent averages already reflect. Only minutes a player was actually
    playing this season are minutes the rotation now has to replace.
    """
    start = f"{season_year - 1}-09-01"      # ESPN labels a season by the year it ends
    cur = [g["minutes"] for g in games
           if (g.get("date") or "") >= start and g.get("minutes") is not None][:n]
    return round(sum(cur) / len(cur), 1) if cur else 0.0


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
