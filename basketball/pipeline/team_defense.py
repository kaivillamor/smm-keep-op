"""
Opponent defense, per market: what a team ALLOWS, relative to the league.

ESPN's team statistics carry only a team's OWN numbers — its "defensive" category is its
own steals, blocks and defensive rebounds — so there is no "points allowed" split to
read. This builds one from the scoreboard instead: every final regular-season game's team
box totals go into `team_games`, and what a defense allowed is what its opponents
recorded against it.

STORED, not cached. A finished slate's scoreboard never changes, so each date is written
once to the NBA research DB on the volume and survives redeploys. The first run backfills
last season (~210 scoreboard calls, once); after that it is a call or two a day.

Market-specific on purpose: a team can be stingy on threes and generous on the glass,
and a single defensive rating would average that away. Pace is folded in, since a fast
team allows more of everything.

LOGGED, NOT USED. The model does not read this yet. It earns its way in only if the
graded corpus shows it improves ranking — the same rule as every other input (§24).

PER POSITION (2026-10-29), for the owner's method. Team totals cannot say whether a
defense is tough on guards and soft on centers, and ESPN has no data on who guards whom,
so every player's line from every stored game goes into `player_games` (one box score per
game) and what a defense allows to guards, forwards and centers comes from that. The method
reads it; the model does not.
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

# Games of last season's allowance that a team's current-season figure is shrunk toward.
# Not fitted — the same reasoning as the player prior in stats_fetcher: after a week,
# three or four games of defense is mostly noise, and last season is a better guess than
# either the noise or the league average. By 20 games this season carries half the weight.
PRIOR_GAMES = 20


def _season_dates(season_year: int, through: str) -> list[str]:
    """Candidate dates for one season, through `through` inclusive.

    ESPN labels a season by the year it ENDS in (2026-27 is 2027). October 1 to April 30
    covers the regular season with margin either side. The window only bounds which dates
    are ASKED about; what counts is ESPN's own per-game season type, so a loose window
    costs a few calls and can never mislabel a game. A hardcoded season boundary already
    got the 2026-27 opener wrong once (§ calendar.season_phase).
    """
    start = date(season_year - 1, 10, 1)
    end = min(date(season_year, 4, 30), date.fromisoformat(through))
    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def sync_team_games(conn, season_year: int, today: str) -> int:
    """Store final regular-season team totals for this season and last, through yesterday.

    Returns the number of games added. A date is marked synced only when the fetch
    SUCCEEDED and every game on it is final — a failed request or a postponed game leaves
    it open, so it is retried rather than skipped forever.
    """
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
                # `final`, not state 'post': a POSTPONED game is also state 'post', but it
                # never completes and has no box. Its makeup arrives later under a new id.
                if g.get("phase") != "regular" or not g.get("final"):
                    continue
                # Standard games ONLY. ESPN tags the All-Star round robin as regular
                # season, and at ~160 points a game its three exhibition "teams" dragged
                # the league average up and deflated every real team's points factor —
                # caught as 33 teams rated in a 30-team league. Allow-listing 'STD' rather
                # than excluding 'ALLSTAR' is deliberate: an unknown new exhibition type
                # then costs a few games instead of corrupting the baseline. It also drops
                # the NBA Cup Championship ('CC'), which doesn't count in the standings.
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
    """{(team_id, stat): factor} — what this defense allows in `stat`, relative to the league.

    1.05 means opponents record 5% more of that stat against them than against an average
    team. This season's per-game allowance is shrunk toward last season's by `k` games,
    then divided by the league mean of those same shrunk figures, so the factors average
    1.0 by construction and stay comparable as the season fills in.
    """
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

# Box scores stored per collect run. The first backfill is ~1,300 games at ~470 KB each,
# once; 250 a run keeps any one run to a few minutes and finishes inside two evenings of
# hourly runs. After that it is the handful of games each night adds.
PLAYER_SYNC_CAP = 250

# A backfill this close to complete ranks the same as a finished one. Without a tolerance,
# a few box scores ESPN never serves would keep the rule off all season.
SYNC_TOLERANCE = 5


def sync_player_games(conn, cap: int = PLAYER_SYNC_CAP) -> int:
    """Store every player's line from each game in `team_games`, newest first.

    `team_games` already holds only the games that count (final, regular season, standard
    games), so this inherits its filters and adds none. A game is marked synced only once
    both of its teams' players parsed: a failed fetch or a one-sided box stays open and is
    retried next run. Newest first, so this season fills before last. Returns games stored.
    """
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
    forwards or centers per 36 minutes, relative to the league. Rank 1 is the stingiest.

    Per minute rather than per game, so it measures the defense and not how many minutes
    opponents happened to give that position against it. This season is shrunk toward last
    by `k` games, as in allowance_factors(), then divided by the league mean, so factors
    average 1.0. Empty until the backfill is done: a half-filled season would rank teams on
    whichever games happened to load first.
    """
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
