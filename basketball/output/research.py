"""
NBA prop-prediction research store: model predictions for every scored player and market,
graded against box scores. Nothing in here is a bet. Twin of baseball/output/research.py:
a logic fix in one needs checking in the other.

    python output/research.py report     # calibration + ranking signal
    python output/research.py owner      # owner logic vs the quant model
    python output/research.py bans       # banned players/teams vs everyone else
    python output/research.py grade      # grade pending predictions
"""
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

# As a script only output/ is on sys.path, so add the package root before the imports.
if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.prop_model import V2_FIT_ROWS, V2_INPUTS
from parlay.method import (CONSISTENCY_L10, CONSISTENCY_L20, METHOD_MARKETS, SIXTH_MAN_MARKETS,
                           SIXTH_MAN_MINUTES, TRUST_MINUTES, untrusted)
from pipeline.calendar import game_slate_date, season_phase, slate_date

RESEARCH_DB = os.getenv("NBA_RESEARCH_DB_PATH") or "data/history/research.db"


def _connect(db_path: str = RESEARCH_DB) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT    NOT NULL,   -- the game's Eastern slate date, from tip-off
            season_phase  TEXT DEFAULT NULL,  -- preseason | regular | postseason | NULL
            game_id       TEXT    NOT NULL,      -- ESPN ids are strings, not ints
            player_id     TEXT    NOT NULL,
            player_name   TEXT,
            team          TEXT,
            opponent      TEXT,
            market        TEXT    NOT NULL,      -- player_points | player_rebounds | ...
            line          REAL,                  -- the book's number; NULL if unpriced
            model_prob    REAL,                  -- final = base_prob + owner_adj
            base_prob     REAL,                  -- quant model only
            owner_adj     REAL,                  -- owner-rules delta
            book_odds     INTEGER,
            book_implied  REAL,
            book          TEXT,                -- which sportsbook priced it
            book_under_odds INTEGER,           -- the same line's under price (from 10-29)
            calibrated_prob REAL,              -- NULL: no NBA calibration is fitted
            -- model inputs
            stats_season  TEXT,                  -- which season(s) the averages came from
            stat_sd       REAL,                  -- sample SD of this player's own log
            season_avg    REAL,                  -- season average for this market
            recent_avg    REAL,                  -- last-N-games average
            recent_games  INTEGER,
            minutes_avg   REAL,                  -- recent minutes per game
            vs_opp_avg    REAL,                  -- this market vs this opponent, both seasons
            vs_opp_games  INTEGER,
            usage_proxy   REAL,                  -- FGA + 0.44*FTA + TOV (ESPN lacks true USG%)
            days_rest     INTEGER,
            is_home       INTEGER,
            opp_allow_factor REAL,               -- what tonight's opponent allows in this
                                                 --  market vs the league (1.05 = 5% more)
            injury_status TEXT,                  -- 'active' | 'out' | 'day-to-day'; NULL =
                                                 --  report unavailable, not healthy
            teammate_minutes_out REAL,           -- this-season minutes of teammates listed
                                                 --  out; NULL = report unavailable
            teammate_usage_out REAL,             -- this-season shots (FGA + 0.44*FTA + TOV)
                                                 --  of teammates listed out; NULL as above
            -- The method's rule inputs at this row's main line
            hit_l10       REAL,                   -- share of last 10 games over this line
            hit_l20       REAL,                   -- share of last 20
            sim_opp_avg   REAL,                   -- avg vs the 5 defenses most like tonight's
            sim_opp_games INTEGER,                --  opponent in this market (opponent excluded)
            season_avg_raw REAL,                  -- this season alone (last season before
                                                  --  its first game), as the method reads it
            h2h_hits      INTEGER,                -- games over this line vs tonight's
            h2h_meetings  INTEGER,                --  opponent, of every meeting in three seasons
            sim_opp_hits  INTEGER,                -- games over this line, of sim_opp_games
            position      TEXT,                   -- 'G' | 'F' | 'C', ESPN's roster listing
            opp_pos_factor REAL,                  -- what tonight's opponent allows to that
            opp_pos_rank  INTEGER,                --  position per 36 in this market, vs the
                                                  --  league; rank 1 = stingiest of 30.
                                                  --  NULL until the box-score backfill ends
            banned        TEXT,                   -- 'player' | 'team' (method.banned), or
                                                  --  'no'. NULL = logged before 10-29
            outcome       TEXT DEFAULT NULL,      -- 'win' | 'loss' | 'void'
            actual_value  REAL DEFAULT NULL,      -- what the player actually recorded
            created_at    TEXT NOT NULL,          -- first time this row was seen
            open_line     REAL,                   -- the first line and price observed, set
            open_odds     INTEGER,                --  once and never updated
            open_priced_at TEXT,
            updated_at    TEXT,                   -- when line/book_odds were last refreshed
            UNIQUE(date, game_id, player_id, market)
        );

        -- Final regular-season team box totals, one row per team per game.
        CREATE TABLE IF NOT EXISTS team_games (
            game_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            season_year  INTEGER,
            team_id      TEXT NOT NULL,
            team         TEXT,
            opponent_id  TEXT NOT NULL,
            opponent     TEXT,
            points       REAL,
            rebounds     REAL,
            assists      REAL,
            threes       REAL,
            PRIMARY KEY (game_id, team_id)
        );
        -- Dates whose games are all final and stored, so the backfill never refetches.
        CREATE TABLE IF NOT EXISTS team_sync (
            date       TEXT PRIMARY KEY,
            synced_at  TEXT
        );

        -- Every player's line from each team_games game, with ESPN's position: the raw
        -- material for defense vs position (team_defense.position_factors).
        CREATE TABLE IF NOT EXISTS player_games (
            game_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            season_year  INTEGER,
            team_id      TEXT NOT NULL,
            opponent_id  TEXT NOT NULL,
            player_id    TEXT NOT NULL,
            position     TEXT,                  -- 'G' | 'F' | 'C'
            minutes      REAL,
            points       REAL,
            rebounds     REAL,
            assists      REAL,
            threes       REAL,
            PRIMARY KEY (game_id, player_id)
        );
        -- Games whose box score is stored, both teams' players parsed.
        CREATE TABLE IF NOT EXISTS player_sync (
            game_id    TEXT PRIMARY KEY,
            synced_at  TEXT
        );

        -- The method's paper parlays, one per tier per slate (parlay/method.py).
        CREATE TABLE IF NOT EXISTS method_parlays (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            date           TEXT NOT NULL,
            tier           TEXT NOT NULL,        -- '$5' | '$10'
            stake          REAL NOT NULL,
            n_legs         INTEGER,
            decimal_odds   REAL,
            american_odds  INTEGER,
            profit_if_win  REAL,
            record_score   REAL,                 -- product of smoothed hit rates: a RANKING
                                                 --  score, not a probability
            first_tip      TEXT,
            generated_at   TEXT NOT NULL,
            outcome        TEXT,                 -- 'win' | 'loss' | 'void'
            profit         REAL,
            UNIQUE(date, tier)
        );
        CREATE TABLE IF NOT EXISTS method_legs (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            parlay_id          INTEGER NOT NULL REFERENCES method_parlays(id),
            game_id            TEXT,
            player_id          TEXT,
            player_name        TEXT,
            team               TEXT,
            opponent           TEXT,
            tipoff             TEXT,
            market             TEXT,
            line               REAL,             -- the line actually picked
            main_line          REAL,             -- the main line it undercuts
            undercut           REAL,             -- 0 = main line
            odds               INTEGER,
            hit_l10            REAL,
            hit_l20            REAL,
            minutes_avg        REAL,
            season_avg         REAL,
            recent_avg         REAL,
            h2h_avg            REAL,
            h2h_games          INTEGER,
            sim_avg            REAL,
            sim_games          INTEGER,
            teammate_usage_out REAL,
            h2h_hits           INTEGER,          -- over the picked line, of h2h_meetings
            h2h_meetings       INTEGER,
            sim_hits           INTEGER,          -- over the picked line, of sim_games
            position           TEXT,
            opp_pos_rank       INTEGER,          -- 1 = stingiest vs his position
            outcome            TEXT,             -- 'win' | 'loss' | 'void'
            actual_value       REAL
        );
    """)
    # Migrations; rows logged before a column existed keep it NULL.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    if "opp_def_rating" in cols and "opp_allow_factor" not in cols:
        conn.execute("ALTER TABLE predictions RENAME COLUMN opp_def_rating TO opp_allow_factor")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    for col, decl in (("open_line", "REAL"), ("open_odds", "INTEGER"),
                      ("open_priced_at", "TEXT"), ("updated_at", "TEXT"),
                      ("injury_status", "TEXT"), ("teammate_minutes_out", "REAL"),
                      ("teammate_usage_out", "REAL"), ("hit_l10", "REAL"),
                      ("hit_l20", "REAL"), ("sim_opp_avg", "REAL"),
                      ("sim_opp_games", "INTEGER"), ("season_avg_raw", "REAL"),
                      ("book_under_odds", "INTEGER"), ("h2h_hits", "INTEGER"),
                      ("h2h_meetings", "INTEGER"), ("sim_opp_hits", "INTEGER"),
                      ("position", "TEXT"), ("opp_pos_factor", "REAL"),
                      ("opp_pos_rank", "INTEGER"), ("banned", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {decl}")
    leg_cols = {r[1] for r in conn.execute("PRAGMA table_info(method_legs)")}
    for col, decl in (("h2h_hits", "INTEGER"), ("h2h_meetings", "INTEGER"),
                      ("sim_hits", "INTEGER"), ("position", "TEXT"),
                      ("opp_pos_rank", "INTEGER")):
        if col not in leg_cols:
            conn.execute(f"ALTER TABLE method_legs ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


# A prediction and its inputs refresh together on conflict: a run with a prediction
# overwrites all of them, a run without one keeps the previous run's.
_RUN_FIELDS = ("model_prob", "base_prob", "owner_adj", "calibrated_prob",
               "stats_season", "stat_sd", "season_avg", "recent_avg", "recent_games",
               "minutes_avg", "usage_proxy", "days_rest", "vs_opp_avg", "vs_opp_games",
               "opp_allow_factor", "hit_l10", "hit_l20", "sim_opp_avg", "sim_opp_games",
               "season_avg_raw", "h2h_hits", "h2h_meetings", "sim_opp_hits", "position",
               "opp_pos_factor", "opp_pos_rank")
_RUN_UPDATE = ", ".join(
    f"{f}=CASE WHEN excluded.model_prob IS NOT NULL THEN excluded.{f} ELSE predictions.{f} END"
    for f in _RUN_FIELDS)

_FIELDS = ("game_id", "player_id", "player_name", "team", "opponent", "market", "line",
           "model_prob", "base_prob", "owner_adj", "calibrated_prob",
           "book_odds", "book_implied", "book", "book_under_odds",
           "stats_season", "stat_sd",
           "season_avg", "recent_avg", "recent_games", "minutes_avg", "vs_opp_avg",
           "vs_opp_games", "usage_proxy", "days_rest", "is_home", "opp_allow_factor",
           "injury_status", "teammate_minutes_out", "teammate_usage_out",
           "hit_l10", "hit_l20", "sim_opp_avg", "sim_opp_games", "season_avg_raw",
           "h2h_hits", "h2h_meetings", "sim_opp_hits", "position", "opp_pos_factor",
           "opp_pos_rank", "banned")


def log_predictions(candidates: list[dict], db_path: str = RESEARCH_DB) -> int:
    """Record every scored player/market. Idempotent: a re-run updates rows in place."""
    if not candidates:
        return 0
    conn = _connect(db_path)
    default_date = slate_date()   # only for a candidate carrying no tipoff of its own
    now = datetime.now(timezone.utc).isoformat()
    cols = ", ".join(("date", "season_phase") + _FIELDS +
                     ("created_at", "open_line", "open_odds", "open_priced_at", "updated_at"))
    marks = ", ".join("?" * (len(_FIELDS) + 7))
    sql = (f"INSERT INTO predictions ({cols}) VALUES ({marks}) "
           "ON CONFLICT(date, game_id, player_id, market) DO UPDATE SET "
           f"  {_RUN_UPDATE}, "
           "  season_phase=COALESCE(excluded.season_phase, predictions.season_phase), "
           "  line=COALESCE(excluded.line, predictions.line), "
           "  book_odds=COALESCE(excluded.book_odds, predictions.book_odds), "
           "  book_implied=COALESCE(excluded.book_implied, predictions.book_implied), "
           # `book` must refresh with the odds it describes.
           "  book=COALESCE(excluded.book, predictions.book), "
           # The under refreshes only with the over, so a stored pair is always from one run.
           "  book_under_odds=CASE WHEN excluded.book_odds IS NOT NULL "
           "    THEN excluded.book_under_odds ELSE predictions.book_under_odds END, "
           # The open columns keep the existing value: the first price seen sticks.
           "  open_line=COALESCE(predictions.open_line, excluded.open_line), "
           "  open_odds=COALESCE(predictions.open_odds, excluded.open_odds), "
           "  open_priced_at=COALESCE(predictions.open_priced_at, excluded.open_priced_at), "
           # Injury fields keep the previous value only when this run's report was NULL.
           "  injury_status=COALESCE(excluded.injury_status, predictions.injury_status), "
           "  teammate_minutes_out=COALESCE(excluded.teammate_minutes_out, "
           "                                predictions.teammate_minutes_out), "
           "  teammate_usage_out=COALESCE(excluded.teammate_usage_out, "
           "                              predictions.teammate_usage_out), "
           "  banned=excluded.banned, "
           "  updated_at=excluded.updated_at")
    n = 0
    slates: set[str] = set()
    for c in candidates:
        game_date = c.get("game_date") or game_slate_date(c.get("tipoff")) or default_date
        slates.add(game_date)
        phase = c.get("season_phase") or season_phase(game_date)
        priced = c.get("book_odds") is not None
        conn.execute(sql, (game_date, phase, *(c.get(f) for f in _FIELDS), now,
                           c.get("line") if priced else None,
                           c.get("book_odds"), now if priced else None, now))
        n += 1
    conn.commit()
    conn.close()
    print(f"[research] logged {n} prediction(s) for {', '.join(sorted(slates)) or default_date} "
          f"(research only — not bets)")
    return n


def grade_predictions(db_path: str = RESEARCH_DB) -> int:
    """Grade pending predictions from ESPN box scores: one box score per game, final games
    only, and a player who did not appear is 'void', never 'loss'."""
    from parlay.method import grade as grade_method
    from pipeline.stats_fetcher import MARKET_STAT, fetch_boxscore, fetch_scoreboard

    conn = _connect(db_path)
    pending = [dict(r) for r in conn.execute(
        "SELECT id, date, game_id, player_id, market, line FROM predictions "
        "WHERE outcome IS NULL")]
    method_dates = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM method_parlays WHERE outcome IS NULL")}
    if not pending and not method_dates:
        conn.close()
        print("[research] no pending predictions.")
        return 0

    # One box score per game, shared by the predictions and the method's legs.
    boxes: dict[str, dict] = {}

    def box_for(game_id: str) -> dict:
        if game_id not in boxes:
            boxes[game_id] = fetch_boxscore(game_id)
        return boxes[game_id]

    finals: dict[str, bool] = {}
    called_off: set[str] = set()
    for day in sorted({p["date"] for p in pending} | method_dates):
        for g in fetch_scoreboard(day):
            finals[str(g["game_id"])] = g["final"]
            # Postponed or cancelled: ESPN says 'post' but never completes it. Void it.
            if g.get("state") == "post" and not g["final"]:
                called_off.add(str(g["game_id"]))

    by_game: dict[str, list] = {}
    for p in pending:
        by_game.setdefault(str(p["game_id"]), []).append(p)

    graded = skipped = 0
    for game_id, rows in by_game.items():
        if game_id in called_off:
            for r in rows:
                conn.execute("UPDATE predictions SET outcome='void' WHERE id=?", (r["id"],))
                graded += 1
            continue
        if not finals.get(game_id):
            skipped += len(rows)
            continue
        box = box_for(game_id)
        if not box:
            skipped += len(rows)
            continue
        for r in rows:
            stat = MARKET_STAT.get(r["market"])
            line = r["line"]
            line_val = None if line is None else float(line)
            player = box.get(str(r["player_id"]))
            actual = None if (player is None or stat is None) else player.get(stat)

            if actual is None or line_val is None:
                outcome, actual_value = "void", actual
            else:
                outcome = "win" if actual > line_val else "loss"
                actual_value = actual

            conn.execute(
                "UPDATE predictions SET outcome=?, actual_value=? WHERE id=?",
                (outcome, actual_value, r["id"]))
            graded += 1

    conn.commit()
    method_graded = grade_method(conn, finals, called_off, box_for)
    conn.close()
    print(f"[research] graded {graded}, skipped {skipped} (game not final yet); "
          f"method legs graded {method_graded}")
    return graded


def _ranking_gap(rows: list[dict]) -> tuple[float, float, float, float, float]:
    """(lower-half hit rate, upper-half hit rate, gap, CI low, CI high), the last three in
    points. Upper and lower halves by model probability; needs at least two rows."""
    rows = sorted(rows, key=lambda r: r["p"])
    n, half = len(rows), len(rows) // 2
    lo = sum(r["y"] for r in rows[:half]) / half
    hi = sum(r["y"] for r in rows[half:]) / (n - half)
    gap = (hi - lo) * 100
    se = math.sqrt(lo * (1 - lo) / half + hi * (1 - hi) / (n - half)) * 100
    return lo, hi, gap, gap - 1.96 * se, gap + 1.96 * se


def _auc(scores: list[float], ys: list[int]) -> tuple[float, float, float]:
    """(AUC, CI low, CI high): the chance a random over outscores a random under, 0.5 a
    coin flip. Tied scores share their average rank; Hanley–McNeil interval. Twin of
    web/app.py's _auc: change both."""
    order = sorted(range(len(scores)), key=scores.__getitem__)
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in order[i:j + 1]:
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    n1 = sum(ys)
    n0 = len(ys) - n1
    if not n1 or not n0:
        return math.nan, math.nan, math.nan
    a = (sum(r for r, y in zip(ranks, ys) if y) - n1 * (n1 + 1) / 2) / (n1 * n0)
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    se = math.sqrt((a * (1 - a) + (n1 - 1) * (q1 - a * a) + (n0 - 1) * (q2 - a * a))
                   / (n1 * n0))
    return a, a - 1.96 * se, a + 1.96 * se


def _inverse(m: list[list[float]]) -> list[list[float]]:
    """Gauss–Jordan inverse of a small square matrix, with partial pivoting."""
    n = len(m)
    a = [row[:] + [float(i == j) for j in range(n)] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        a[c] = [v / a[c][c] for v in a[c]]
        for r in range(n):
            if r != c and a[r][c]:
                f = a[r][c]
                a[r] = [v - f * u for v, u in zip(a[r], a[c])]
    return [row[n:] for row in a]


def _logit_fit(xs: list[list[float]], ys: list[int]) -> tuple[list[float], list[float]]:
    """Logistic regression by Newton–Raphson: (coefficients, standard errors), intercept
    first. Pure Python: numpy is not deployed."""
    k = len(xs[0]) + 1
    w = [0.0] * k
    for _ in range(50):
        g = [0.0] * k
        h = [[0.0] * k for _ in range(k)]
        for x, y in zip(xs, ys):
            row = (1.0, *x)
            p = 1 / (1 + math.exp(-sum(a * b for a, b in zip(w, row))))
            for i in range(k):
                g[i] += (y - p) * row[i]
                for j in range(k):
                    h[i][j] += p * (1 - p) * row[i] * row[j]
        cov = _inverse(h)
        step = [sum(c * gi for c, gi in zip(cov_row, g)) for cov_row in cov]
        w = [a + s for a, s in zip(w, step)]
        if max(abs(s) for s in step) < 1e-10:
            break
    return w, [math.sqrt(cov[i][i]) for i in range(k)]


def _fair(over: int | None, under: int | None) -> float | None:
    """The book's vig-free P(over), from both prices on one line; None without both."""
    if over is None or under is None:
        return None
    imp = lambda o: abs(o) / (abs(o) + 100) if o < 0 else 100 / (o + 100)
    return imp(over) / (imp(over) + imp(under))


def _book_benchmark(rows: list[dict], w: int) -> None:
    """The model against the book's own price on the same graded rows: AUC for each, and
    what the model adds beyond the price."""
    logit = lambda q: math.log(min(max(q, 1e-3), 1 - 1e-3) / (1 - min(max(q, 1e-3), 1 - 1e-3)))
    print(f"{'─' * w}\n  BOOK BENCHMARK (the bar any model must clear)")
    print("    AUC = the chance an over outranks an under; 0.500 is a coin flip")
    print(f"    {'':24}{'n':>5}   {'model AUC':<21}book AUC")
    for label, key in (("over price, all priced", "book_implied"),
                       ("fair price, two-sided", "fair")):
        g = [r for r in rows if r[key] is not None]
        if len(g) < 200:
            print(f"    {label:24}{len(g):>5}   too few to read")
            continue
        ys = [r["y"] for r in g]
        m, m_lo, m_hi = _auc([r["p"] for r in g], ys)
        b, b_lo, b_hi = _auc([r[key] for r in g], ys)
        print(f"    {label:24}{len(g):>5}   {m:.3f} [{m_lo:.3f}, {m_hi:.3f}]  "
              f"{b:.3f} [{b_lo:.3f}, {b_hi:.3f}]")
        coef, se = _logit_fit([[logit(r[key]), logit(r["p"])] for r in g], ys)
        lo, hi = coef[2] - 1.96 * se[2], coef[2] + 1.96 * se[2]
        verdict = ("adds to the price" if lo > 0 else
                   "points the wrong way once the price is known" if hi < 0 else
                   "adds nothing beyond the price")
        print(f"      model beyond the price: {coef[2]:+.3f}, 95% CI [{lo:+.3f}, {hi:+.3f}]")
        print(f"      →  {verdict}")
    side = sum(r["y"] if r["p"] > 0.5 else 1 - r["y"] for r in rows) / len(rows)
    under = 1 - sum(r["y"] for r in rows) / len(rows)
    print(f"    the model's side wins {side * 100:.1f}%; "
          f"the under every time wins {under * 100:.1f}%")


def report(db_path: str = RESEARCH_DB) -> None:
    """Calibration and ranking-signal report, led by the method's player pools."""
    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT model_prob p, outcome, market, minutes_avg, hit_l10, hit_l20, book_implied, "
        "       book_odds, book_under_odds "
        "FROM predictions WHERE outcome IN ('win','loss') AND model_prob IS NOT NULL")]
    total = conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"]
    # Rows v2's pre-registered fit may use: graded, priced on both sides, every input.
    v2_rows = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE outcome IN ('win','loss') "
        "AND book_odds IS NOT NULL AND book_under_odds IS NOT NULL AND "
        + " AND ".join(f"{c} IS NOT NULL" for c in V2_INPUTS)).fetchone()[0]
    conn.close()
    for r in rows:
        r["fair"] = _fair(r["book_odds"], r["book_under_odds"])

    w = 72
    print(f"\n{'=' * w}\n  MODEL CALIBRATION (research only — not bets)\n{'=' * w}")
    print(f"  predictions logged : {total}")
    print(f"  graded             : {len(rows)}")
    if len(rows) < 20:
        print(f"\n  Need ~200+ graded predictions before this means anything.")
        print(f"{'=' * w}\n")
        return

    n = len(rows)
    for r in rows:
        r["y"] = 1 if r["outcome"] == "win" else 0

    # The method's player filters, then wider.
    trusted = [r for r in rows if untrusted(r["minutes_avg"], r["market"]) is None]
    pool = [r for r in trusted if r["hit_l10"] is not None and r["hit_l20"] is not None
            and r["hit_l10"] >= CONSISTENCY_L10 and r["hit_l20"] >= CONSISTENCY_L20]
    words = lambda markets: "/".join(m.split("_", 1)[1] for m in markets)
    print(f"{'─' * w}\n  BY PLAYER POOL (the method's filters first)")
    print(f"    trusted = {TRUST_MINUTES:g}+ min in {words(METHOD_MARKETS)}, "
          f"{SIXTH_MAN_MINUTES:g}+ in {words(SIXTH_MAN_MARKETS)}")
    print(f"    {'':22}{'n':>5}  {'model':>6} {'hit':>6} {'priced':>6}{'gap':>7}  95% CI")
    for label, g in ((f"trusted, {CONSISTENCY_L10 * 10:g}/10, {CONSISTENCY_L20 * 20:g}/20",
                      pool), ("trusted", trusted), ("everyone", rows)):
        if len(g) < 20:
            print(f"    {label:22}{len(g):>5}  too few graded to read")
            continue
        priced = [r["book_implied"] for r in g if r["book_implied"] is not None]
        _, _, gap, ci_lo, ci_hi = _ranking_gap(g)
        print(f"    {label:22}{len(g):>5}  {sum(r['p'] for r in g) / len(g) * 100:5.1f}%"
              f" {sum(r['y'] for r in g) / len(g) * 100:5.1f}%"
              f" {sum(priced) / len(priced) * 100 if priced else float('nan'):5.1f}%"
              f"  {gap:+5.1f}  [{ci_lo:+.1f}, {ci_hi:+.1f}]")

    print(f"{'─' * w}\n  EVERYONE")
    actual = sum(r["y"] for r in rows) / n
    mean_p = sum(r["p"] for r in rows) / n
    print(f"  model says         : {mean_p * 100:.1f}%")
    print(f"  actually hit       : {actual * 100:.1f}%")
    print(f"  overconfidence     : {(mean_p - actual) * 100:+.1f} pts")

    print(f"{'─' * w}\n  calibration by predicted bucket")
    for lo, hi in ((0.0, 0.55), (0.55, 0.65), (0.65, 0.72), (0.72, 0.80), (0.80, 1.01)):
        b = [r for r in rows if lo <= r["p"] < hi]
        if not b:
            continue
        hit = sum(r["y"] for r in b) / len(b)
        pm  = sum(r["p"] for r in b) / len(b)
        print(f"    {lo*100:3.0f}-{hi*100:3.0f}%: n={len(b):4}  predicted={pm*100:5.1f}%  actual={hit*100:5.1f}%")

    lo_rate, hi_rate, gap, ci_lo, ci_hi = _ranking_gap(rows)
    print(f"{'─' * w}\n  RANKING SIGNAL (the test that matters)")
    print(f"    lower half by model prob → hit {lo_rate * 100:.1f}%")
    print(f"    upper half by model prob → hit {hi_rate * 100:.1f}%")
    verdict = ("inverted — ranks WORSE than chance" if ci_hi < 0 else
               "inconclusive — interval still contains zero" if ci_lo <= 0 else
               "usable signal" if gap >= 8 else "weak but real signal")
    print(f"    gap: {gap:+.1f} pts   95% CI [{ci_lo:+.1f}, {ci_hi:+.1f}]")
    print(f"    →  {verdict}")
    _book_benchmark(rows, w)
    print(f"{'─' * w}\n  v2 (pre-registered 2026-10-28, model/prop_model.py): "
          f"{v2_rows:,} of {V2_FIT_ROWS:,}\n    qualifying graded rows before its fit")
    print(f"{'=' * w}\n")


def compare_owner_logic(db_path: str = RESEARCH_DB) -> None:
    """A/B the quant model (base_prob) against model + owner rules (model_prob), paired on
    the same graded rows."""
    import math

    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT model_prob, base_prob, owner_adj, outcome FROM predictions "
        "WHERE outcome IN ('win','loss') AND base_prob IS NOT NULL "
        "  AND model_prob IS NOT NULL")]
    conn.close()

    w = 56
    print(f"\n{'=' * w}\n  QUANT MODEL  vs  QUANT + YOUR LOGIC\n{'=' * w}")
    if not rows:
        print("  No rows with base_prob yet — logging started 2026-09-11.")
        print("  Rows from before then only have the combined number, so they")
        print("  cannot be split. This fills in from the next slate onward.")
        print(f"{'=' * w}\n")
        return

    fired = [r for r in rows if (r["owner_adj"] or 0) != 0]
    print(f"  graded rows          : {len(rows)}")
    print(f"  personal rule fired  : {len(fired)}  ({len(fired)/len(rows):.1%})")
    if not fired:
        print("\n  No rule has fired yet — the two arms are identical, nothing to compare.")
        print(f"{'=' * w}\n")
        return

    def metrics(key):
        ps = [r[key] for r in rows]
        ys = [1 if r["outcome"] == "win" else 0 for r in rows]
        n = len(ps)
        mp, my = sum(ps) / n, sum(ys) / n
        num = sum((p - mp) * (y - my) for p, y in zip(ps, ys))
        den = sum((p - mp) ** 2 for p in ps)
        slope = num / den if den else 0.0          # 1.0 = perfectly calibrated
        order = sorted(zip(ps, ys))
        half = n // 2
        lo = sum(y for _, y in order[:half]) / half if half else 0
        hi = sum(y for _, y in order[half:]) / (n - half) if n - half else 0
        return {"pred": mp, "actual": my, "slope": slope, "gap": hi - lo}

    a, b = metrics("base_prob"), metrics("model_prob")
    print(f"\n  {'':<22}{'quant only':>13}{'+ your logic':>15}{'delta':>9}")
    for label, k, fmt in (("predicted", "pred", "pct"), ("actually hit", "actual", "pct"),
                          ("calibration slope", "slope", "raw"), ("ranking gap", "gap", "pts")):
        va, vb = a[k], b[k]
        if fmt == "pct":
            print(f"  {label:<22}{va:>12.1%}{vb:>15.1%}{(vb-va)*100:>+8.1f}pt")
        elif fmt == "pts":
            print(f"  {label:<22}{va*100:>11.1f}pt{vb*100:>14.1f}pt{(vb-va)*100:>+8.1f}pt")
        else:
            print(f"  {label:<22}{va:>12.3f}{vb:>15.3f}{vb-va:>+9.3f}")

    # Did the flagged legs actually outperform? Paired on the same outcomes.
    hit = lambda g: sum(1 for r in g if r["outcome"] == "win") / len(g)
    rest = [r for r in rows if (r["owner_adj"] or 0) == 0]
    print(f"\n  legs your rules touched : {hit(fired):.1%}  (n={len(fired)})")
    if rest:
        print(f"  every other leg        : {hit(rest):.1%}  (n={len(rest)})")
        diff = hit(fired) - hit(rest)
        se = math.sqrt(hit(fired) * (1 - hit(fired)) / len(fired)
                       + hit(rest) * (1 - hit(rest)) / len(rest))
        lo_ci, hi_ci = (diff - 1.96 * se) * 100, (diff + 1.96 * se) * 100
        print(f"  difference             : {diff*100:+.1f} pts   95% CI [{lo_ci:+.1f}, {hi_ci:+.1f}]")
        if lo_ci > 0:
            print("  -> your rules beat the model, and the interval excludes zero.")
        elif hi_ci < 0:
            print("  -> your rules hurt, and the interval excludes zero.")
        else:
            print("  -> inconclusive: the interval still contains zero. Need more rows.")
    print(f"{'=' * w}\n")


def compare_bans(db_path: str = RESEARCH_DB) -> None:
    """Banned players' and teams' props against the rest of the trusted pool. `banned` is
    NULL before the lists began and 'no' when on neither list."""
    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT banned, outcome, minutes_avg, market FROM predictions "
        "WHERE outcome IN ('win','loss') AND banned IS NOT NULL")]
    conn.close()
    pool = [r for r in rows if untrusted(r["minutes_avg"], r["market"]) is None]
    ban = [r for r in pool if r["banned"] != "no"]
    rest = [r for r in pool if r["banned"] == "no"]

    w = 56
    print(f"\n{'=' * w}\n  BANNED vs EVERYONE ELSE (trusted pool)\n{'=' * w}")
    if len(ban) < 20 or not rest:
        print(f"  {len(ban)} graded banned row(s) — too few to read (need 20).")
        print(f"{'=' * w}\n")
        return
    hit = lambda g: sum(1 for r in g if r["outcome"] == "win") / len(g)
    a, b = hit(ban), hit(rest)
    se = math.sqrt(a * (1 - a) / len(ban) + b * (1 - b) / len(rest))
    print(f"  banned        : {a:.1%} over  (n={len(ban)})")
    print(f"  everyone else : {b:.1%} over  (n={len(rest)})")
    print(f"  difference    : {(a - b) * 100:+.1f} pts   "
          f"95% CI [{(a - b - 1.96 * se) * 100:+.1f}, {(a - b + 1.96 * se) * 100:+.1f}]")
    print(f"{'=' * w}\n")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "grade":
        grade_predictions()
    elif cmd in ("owner", "compare"):
        compare_owner_logic()
    elif cmd == "bans":
        compare_bans()
    else:
        report()
        compare_owner_logic()
        compare_bans()
