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
"""
from datetime import date, datetime, timedelta, timezone

from pipeline.stats_fetcher import parse_scoreboard, scoreboard_data

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
