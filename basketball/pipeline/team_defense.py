"""
Opponent defense per market: what a team allows, relative to the league, built from stored
box totals (ESPN's team stats carry only a team's own numbers). Team totals go into
`team_games`; every player's line goes into `player_games` for the per-position figures.
"""
from datetime import date, datetime, timedelta, timezone

from pipeline.stats_fetcher import fetch_boxscore, parse_scoreboard, scoreboard_data

TEAM_STATS = ("points", "rebounds", "assists", "threes")

MARKET_TEAM_STAT = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_made_threes": "threes",
}

# Last season's allowance counts as this many games of this season's.
PRIOR_GAMES = 20


def _season_dates(season_year: int, through: str) -> list[str]:
    """Dates to ask about for one season (labelled by the year it ends): October 1 to
    April 30, or `through`. ESPN's per-game season type decides what counts."""
    start = date(season_year - 1, 10, 1)
    end = min(date(season_year, 4, 30), date.fromisoformat(through))
    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def sync_team_games(conn, season_year: int, today: str) -> int:
    """Store final regular-season team totals for this season and last, through yesterday;
    returns games added. A date is marked synced only when its fetch succeeded and every
    game on it is final."""
    synced = {r[0] for r in conn.execute("SELECT date FROM team_sync")}
    through = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    added = 0
    for year in (season_year - 1, season_year):
        for day in _season_dates(year, through):
            if day in synced:
                continue
            data = scoreboard_data(day)
            if data is None:
                continue                      # failed fetch: try again next run
            games = parse_scoreboard(data)
            for g in games:
                # `final`, not state 'post': a postponed game is 'post' too.
                if g.get("phase") != "regular" or not g.get("final"):
                    continue
                # Standard games only: ESPN tags All-Star games as regular season.
                if g.get("comp_type") != "STD":
                    continue
                if not (g.get("home_box") and g.get("away_box")):
                    continue
                for team_id, team, opp_id, opp, box in (
                        (g["home_id"], g["home"], g["away_id"], g["away"], g["home_box"]),
                        (g["away_id"], g["away"], g["home_id"], g["home"], g["away_box"])):
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO team_games (game_id, date, season_year, "
                        "team_id, team, opponent_id, opponent, points, rebounds, assists, "
                        "threes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (g["game_id"], day, g.get("season_year"), team_id, team, opp_id,
                         opp, box["points"], box["rebounds"], box["assists"], box["threes"]))
                    added += cur.rowcount
            if all(g.get("state") == "post" for g in games):
                conn.execute("INSERT OR IGNORE INTO team_sync (date, synced_at) VALUES (?, ?)",
                             (day, datetime.now(timezone.utc).isoformat()))
            conn.commit()                     # per date: a crash mid-backfill keeps progress
    return added // 2


def allowance_factors(conn, season_year: int, k: int = PRIOR_GAMES) -> dict:
    """{(team_id, stat): factor}: what this defense allows in `stat` relative to the league
    (1.05 = 5% more than average). This season is shrunk toward last by `k` games."""
    totals: dict[tuple, list] = {}
    for yr, opp, *vals in conn.execute(
            "SELECT season_year, opponent_id, points, rebounds, assists, threes "
            "FROM team_games WHERE season_year IN (?, ?)", (season_year - 1, season_year)):
        for stat, v in zip(TEAM_STATS, vals):
            if v is None:
                continue
            t = totals.setdefault((yr, opp, stat), [0.0, 0])
            t[0] += v
            t[1] += 1

    estimate: dict[tuple, float] = {}
    for opp in {key[1] for key in totals}:
        for stat in TEAM_STATS:
            c_sum, c_n = totals.get((season_year, opp, stat), (0.0, 0))
            p_sum, p_n = totals.get((season_year - 1, opp, stat), (0.0, 0))
            if p_n:
                estimate[(opp, stat)] = (c_sum + k * p_sum / p_n) / (c_n + k)
            elif c_n:
                estimate[(opp, stat)] = c_sum / c_n

    factors = {}
    for stat in TEAM_STATS:
        vals = [v for (o, s), v in estimate.items() if s == stat]
        if not vals:
            continue
        league = sum(vals) / len(vals)
        for (o, s), v in estimate.items():
            if s == stat and league:
                factors[(o, s)] = round(v / league, 4)
    return factors


# ── per position ─────────────────────────────────────────────────────────────

POSITIONS = ("G", "F", "C")

PLAYER_SYNC_CAP = 250   # box scores stored per collect run
SYNC_TOLERANCE = 5      # missing games allowed before the backfill counts as done


def sync_player_games(conn, cap: int = PLAYER_SYNC_CAP) -> int:
    """Store every player's line from each game in `team_games`, newest first; returns
    games stored. A game is marked synced only once both teams' players parsed."""
    todo = conn.execute(
        "SELECT game_id, MIN(date), MIN(season_year) FROM team_games "
        "WHERE game_id NOT IN (SELECT game_id FROM player_sync) "
        "GROUP BY game_id ORDER BY MIN(date) DESC LIMIT ?", (cap,)).fetchall()
    stored = 0
    for game_id, day, season_year in todo:
        sides = dict(conn.execute(
            "SELECT team_id, opponent_id FROM team_games WHERE game_id=?", (game_id,)))
        box = fetch_boxscore(game_id)
        if not box or set(sides) - {p["team_id"] for p in box.values()}:
            continue
        for pid, p in box.items():
            if p["team_id"] not in sides:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO player_games (game_id, date, season_year, team_id, "
                "opponent_id, player_id, position, minutes, points, rebounds, assists, "
                "threes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (game_id, day, season_year, p["team_id"], sides[p["team_id"]], pid,
                 p["position"], p["minutes"], p["points"], p["rebounds"], p["assists"],
                 p["threes_made"]))
        conn.execute("INSERT OR IGNORE INTO player_sync (game_id, synced_at) VALUES (?, ?)",
                     (game_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()                         # per game: a crash mid-backfill keeps progress
        stored += 1
    return stored


def position_sync_status(conn, season_year: int) -> tuple[int, int]:
    """(games stored, games that count) for this season and last."""
    return conn.execute(
        "SELECT COUNT(DISTINCT s.game_id), COUNT(DISTINCT t.game_id) FROM team_games t "
        "LEFT JOIN player_sync s ON s.game_id = t.game_id WHERE t.season_year IN (?, ?)",
        (season_year - 1, season_year)).fetchone()


def position_factors(conn, season_year: int, k: int = PRIOR_GAMES) -> dict:
    """{(team_id, position, stat): (factor, rank)}: what a defense allows to guards,
    forwards or centers per 36 minutes, relative to the league; rank 1 is the stingiest.
    Shrunk as in allowance_factors(). Empty until the backfill is done."""
    stored, total = position_sync_status(conn, season_year)
    if not total or total - stored > SYNC_TOLERANCE:
        return {}

    totals: dict[tuple, list] = {}            # (year, defense, pos, stat) -> [stat, minutes]
    games: dict[tuple, set] = {}              # (year, defense) -> game ids
    for yr, opp, pos, gid, mins, *vals in conn.execute(
            "SELECT season_year, opponent_id, position, game_id, minutes, points, rebounds, "
            "assists, threes FROM player_games WHERE season_year IN (?, ?) "
            "AND position IS NOT NULL AND minutes > 0", (season_year - 1, season_year)):
        games.setdefault((yr, opp), set()).add(gid)
        for stat, v in zip(TEAM_STATS, vals):
            if v is None:
                continue
            t = totals.setdefault((yr, opp, pos, stat), [0.0, 0.0])
            t[0] += v
            t[1] += mins

    rates: dict[tuple, float] = {}
    for opp in {d for _, d in games}:
        p_n = len(games.get((season_year - 1, opp), ()))
        for pos in POSITIONS:
            for stat in TEAM_STATS:
                c_v, c_m = totals.get((season_year, opp, pos, stat), (0.0, 0.0))
                p_v, p_m = totals.get((season_year - 1, opp, pos, stat), (0.0, 0.0))
                if p_n:
                    v, m = c_v + k * p_v / p_n, c_m + k * p_m / p_n
                else:
                    v, m = c_v, c_m
                if m:
                    rates[(opp, pos, stat)] = 36 * v / m

    out = {}
    for pos in POSITIONS:
        for stat in TEAM_STATS:
            ranked = sorted((r, o) for (o, p, s), r in rates.items() if p == pos and s == stat)
            if not ranked:
                continue
            league = sum(r for r, _ in ranked) / len(ranked)
            for i, (r, o) in enumerate(ranked, 1):
                out[(o, pos, stat)] = (round(r / league, 4), i)
    return out
