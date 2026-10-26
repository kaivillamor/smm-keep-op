"""
NBA prop-prediction research store — copied from baseball/output/research.py 2026-09-12.

DELIBERATELY SEPARATE from any betting record, exactly as in baseball: nothing in here is
a bet. These are model predictions for every player scored on a slate, graded against box
scores, so we can measure whether the probabilities mean anything.

SHARED ORIGIN. `report()` and `compare_owner_logic()` are verbatim copies — they only read
model_prob / base_prob / owner_adj / outcome, which every sport has. Fix a logic bug in one
and check the twin (PROJECT.md §25).

SCHEMA DIFFERS from baseball, because the prediction is a different shape:

    baseball : "P(1+ hit)"          -> binary, no line, outcome from actual_hits >= 1
    NBA      : "P(value > line)"    -> needs market + line + actual_value

So a row carries `market` ('player_points'), `line` (24.5) and `actual_value` (27). A
prediction without its line is meaningless, which is not true in baseball.

    python output/research.py report     # calibration + ranking signal
    python output/research.py owner      # your logic vs the quant model
    python output/research.py grade      # grade pending predictions
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Run as a script (see the docstring's CLI lines) only output/ lands on sys.path, so
# `pipeline` would not import. Put the package root on the path BEFORE the import below.
# The baseball twin learned this by breaking `research.py report` outright.
if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
            date          TEXT    NOT NULL,   -- the GAME's Eastern slate date, from
                                              --  tip-off, NOT the date collection ran.
                                              --  Baseball used the collection date here
                                              --  and duplicated 20% of its corpus (§29).
            season_phase  TEXT DEFAULT NULL,  -- preseason | regular | postseason | NULL.
                                              --  Preseason minutes are not real minutes
                                              --  and minutes drive every prop here.
            game_id       TEXT    NOT NULL,      -- ESPN ids are strings, not ints
            player_id     TEXT    NOT NULL,
            player_name   TEXT,
            team          TEXT,
            opponent      TEXT,
            market        TEXT    NOT NULL,      -- player_points | player_rebounds | ...
            line          REAL,                  -- the book's number; NULL if unpriced
            -- predictions: ADD columns, never overwrite (PROJECT.md §24)
            model_prob    REAL,                  -- final = base_prob + owner_adj
            base_prob     REAL,                  -- quant model only
            owner_adj     REAL,                  -- personal-logic delta, kept measurable
            book_odds     INTEGER,
            book_implied  REAL,
            book          TEXT,                -- WHICH sportsbook priced it. Baseball
                                               --  computed this and threw it away, so a
                                               --  verified-wrong feed stayed invisible
                                               --  for two months (§28). Logged from row
                                               --  one here; an unattributed price is
                                               --  suspect by definition.
            calibrated_prob REAL,              -- model_prob shrunk toward the base rate.
                                               --  The number a betting gate compares,
                                               --  NOT model_prob. Baseball computed it
                                               --  and discarded it, which hid a gate
                                               --  that could not fire for three weeks
                                               --  (§29). There is no gate yet — the
                                               --  column exists so there never can be
                                               --  one whose input is unlogged.
            -- model inputs, stored so a later refit can be run properly
            stats_season  TEXT,                  -- WHICH season the averages came from.
                                               --  On opening night there are no current-
                                               --  season games, so form falls back to
                                               --  last season. Unlogged, that is a silent
                                               --  method change mid-corpus — §24.
            stat_sd       REAL,                  -- sample SD of this player's own log
            season_avg    REAL,                  -- season average for this market
            recent_avg    REAL,                  -- last-N-games average
            recent_games  INTEGER,
            minutes_avg   REAL,                  -- the single biggest NBA prop driver
            vs_opp_avg    REAL,                  -- this market vs this opponent, both
            vs_opp_games  INTEGER,               --  seasons. Logged; noisy at 2-8 games
            usage_proxy   REAL,                  -- FGA + 0.44*FTA + TOV (ESPN lacks true USG%)
            days_rest     INTEGER,               -- back-to-backs matter far more than in MLB
            is_home       INTEGER,
            opp_allow_factor REAL,               -- what tonight's opponent ALLOWS in this
                                                 --  market vs the league (1.05 = 5% more),
                                                 --  shrunk toward last season. See
                                                 --  pipeline/team_defense. Was named
                                                 --  opp_def_rating, never populated, and
                                                 --  renamed 2026-10-26: "defensive rating"
                                                 --  means points per 100 possessions in
                                                 --  the NBA, which this is not.
            injury_status TEXT,                  -- 'active' | 'out' | 'day-to-day' from the
                                                 --  injury report; NULL = report unavailable
                                                 --  that run, NOT "healthy"
            teammate_minutes_out REAL,           -- this-season minutes of teammates listed
                                                 --  out: the role a player may absorb.
                                                 --  NULL = report unavailable
            outcome       TEXT DEFAULT NULL,      -- 'win' | 'loss' | 'void'
            actual_value  REAL DEFAULT NULL,      -- what the player actually recorded
            created_at    TEXT NOT NULL,          -- first time this row was seen
            open_line     REAL,                   -- FIRST line observed. Collection runs
            open_odds     INTEGER,                --  hourly and overwrites line/book_odds,
            open_priced_at TEXT,                  --  so without these the opening number
                                                  --  is lost and line movement — closing
                                                  --  line value, the standard test of
                                                  --  whether a bet had edge — cannot be
                                                  --  measured. Set once, never updated.
            updated_at    TEXT,                   -- when line/book_odds were last refreshed
            UNIQUE(date, game_id, player_id, market)
        );

        -- Final regular-season team box totals, one row per team per game: the raw
        -- material for opp_allow_factor. Stored rather than cached because a finished
        -- slate never changes, and the volume keeps it across redeploys.
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
    """)
    # Migration for databases created before 2026-10-26. Rows logged earlier keep these
    # NULL rather than being backfilled from line/book_odds: those hold the LAST snapshot,
    # not the opening one, and copying them in would record a closing price as an open.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    # Rename, not add-and-abandon: opp_def_rating was NULL on every row ever written, so
    # nothing is lost, and a dead column next to its replacement is bloat.
    if "opp_def_rating" in cols and "opp_allow_factor" not in cols:
        conn.execute("ALTER TABLE predictions RENAME COLUMN opp_def_rating TO opp_allow_factor")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    for col, decl in (("open_line", "REAL"), ("open_odds", "INTEGER"),
                      ("open_priced_at", "TEXT"), ("updated_at", "TEXT"),
                      ("injury_status", "TEXT"), ("teammate_minutes_out", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


# A prediction and the inputs that produced it, refreshed TOGETHER on conflict. When a run
# produces a prediction the row takes everything that run computed; when it doesn't (the
# line was pulled, a log fetch failed) the row keeps the previous run's everything.
#
# Before 2026-10-26 only model_prob refreshed. Game logs are cached per day, so normally
# every run computes identical inputs and the gap was invisible — until code changed
# mid-day: rows written by the afternoon's deploy kept its unblended season averages while
# model_prob was recomputed from the new blended ones. A row whose stored inputs cannot
# reproduce its own prediction is what §24 forbids. The same rule also stops a run with no
# prediction from overwriting a good one with NULL.
_RUN_FIELDS = ("model_prob", "base_prob", "owner_adj", "calibrated_prob",
               "stats_season", "stat_sd", "season_avg", "recent_avg", "recent_games",
               "minutes_avg", "usage_proxy", "days_rest", "vs_opp_avg", "vs_opp_games",
               "opp_allow_factor")
_RUN_UPDATE = ", ".join(
    f"{f}=CASE WHEN excluded.model_prob IS NOT NULL THEN excluded.{f} ELSE predictions.{f} END"
    for f in _RUN_FIELDS)

_FIELDS = ("game_id", "player_id", "player_name", "team", "opponent", "market", "line",
           "model_prob", "base_prob", "owner_adj", "calibrated_prob",
           "book_odds", "book_implied", "book",
           "stats_season", "stat_sd",
           "season_avg", "recent_avg", "recent_games", "minutes_avg", "vs_opp_avg",
           "vs_opp_games", "usage_proxy", "days_rest", "is_home", "opp_allow_factor",
           "injury_status", "teammate_minutes_out")


def log_predictions(candidates: list[dict], db_path: str = RESEARCH_DB) -> int:
    """Records every scored player/market. Idempotent — re-running a slate updates in
    place rather than duplicating, so a mid-day re-run is safe.

    Column list is built from _FIELDS so adding an input cannot desynchronise the
    placeholder count — the baseball twin has hand-written VALUES(?,?,...) lists that had
    to be recounted by hand on every schema change.
    """
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
           # season_phase is derived metadata about the GAME, not a prediction, so a
           # re-run correcting it to ESPN's authoritative value is a fix, not the kind of
           # overwrite §24 forbids. This is what repairs a row first logged from the
           # date-based fallback.
           "  season_phase=COALESCE(excluded.season_phase, predictions.season_phase), "
           "  line=COALESCE(excluded.line, predictions.line), "
           "  book_odds=COALESCE(excluded.book_odds, predictions.book_odds), "
           "  book_implied=COALESCE(excluded.book_implied, predictions.book_implied), "
           # `book` MUST refresh with the odds it describes. Baseball omitted it, so a row
           # that gained a price on a later run kept book=NULL and its provenance backfill
           # then stamped 219 good prices as coming from the discredited feed (§29).
           "  book=COALESCE(excluded.book, predictions.book), "
           # The open columns keep the EXISTING value first — the reverse of the latest-
           # price columns above — so the first non-NULL observation sticks. A row first
           # logged before its price posted gets its open from the first run that sees one.
           "  open_line=COALESCE(predictions.open_line, excluded.open_line), "
           "  open_odds=COALESCE(predictions.open_odds, excluded.open_odds), "
           "  open_priced_at=COALESCE(predictions.open_priced_at, excluded.open_priced_at), "
           # The injury pair keeps the previous value only when this run's report was
           # UNAVAILABLE (NULL) — a player cleared since the last run arrives as 'active',
           # not NULL, and overwrites. Independent of whether a prediction was made.
           "  injury_status=COALESCE(excluded.injury_status, predictions.injury_status), "
           "  teammate_minutes_out=COALESCE(excluded.teammate_minutes_out, "
           "                                predictions.teammate_minutes_out), "
           "  updated_at=excluded.updated_at")
    n = 0
    slates: set[str] = set()
    for c in candidates:
        game_date = c.get("game_date") or game_slate_date(c.get("tipoff")) or default_date
        slates.add(game_date)
        # ESPN's season type when the caller has it, the date calendar only as fallback.
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
    """Grades pending predictions against ESPN box scores.

    Three rules, each learned the hard way in baseball:
      * ONE box score per GAME, not per player — a 200-row slate is ~11 calls, not 200.
      * Only FINAL games. Box scores populate live, and baseball graded a player a loss
        before his game had started by trusting a non-final one.
      * A player who did not appear is 'void', never 'loss'. A DNP is not a failed
        prediction, and scoring it as one would bias every calibration bucket downward.
    """
    from pipeline.stats_fetcher import MARKET_STAT, fetch_boxscore, fetch_scoreboard

    conn = _connect(db_path)
    pending = [dict(r) for r in conn.execute(
        "SELECT id, date, game_id, player_id, market, line FROM predictions "
        "WHERE outcome IS NULL")]
    if not pending:
        conn.close()
        print("[research] no pending predictions.")
        return 0

    finals: dict[str, bool] = {}
    called_off: set[str] = set()
    for day in sorted({p["date"] for p in pending}):
        for g in fetch_scoreboard(day):
            finals[str(g["game_id"])] = g["final"]
            # Postponed or cancelled: ESPN reports state 'post' but never completes it. A
            # book voids props on a game that isn't played, so do the same — otherwise its
            # rows would sit pending forever. A makeup game is a new event with a new id.
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
        box = fetch_boxscore(game_id)
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
    conn.close()
    print(f"[research] graded {graded}, skipped {skipped} (game not final yet)")
    return graded


def report(db_path: str = RESEARCH_DB) -> None:
    """Calibration + ranking-signal report. The ranking test is the important one:
    a model worth betting has its higher-probability half hit MORE often."""
    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT model_prob p, outcome FROM predictions "
        "WHERE outcome IN ('win','loss') AND model_prob IS NOT NULL")]
    total = conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"]
    conn.close()

    w = 56
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

    rows.sort(key=lambda r: r["p"])
    half = n // 2
    lo_rate = sum(r["y"] for r in rows[:half]) / half
    hi_rate = sum(r["y"] for r in rows[half:]) / (n - half)
    print(f"{'─' * w}\n  RANKING SIGNAL (the test that matters)")
    print(f"    lower half by model prob → hit {lo_rate * 100:.1f}%")
    print(f"    upper half by model prob → hit {hi_rate * 100:.1f}%")
    gap = (hi_rate - lo_rate) * 100
    # The INTERVAL decides, not the point estimate. A threshold on the gap alone reads
    # noise as a finding: +6.9 pts on 696 rows carries a 95% CI of [-0.2, +14.0], which
    # is indistinguishable from zero, yet a bare `gap > 3` test calls it real.
    import math
    # lo_rate/hi_rate, NOT lo/hi — those are bucket BOUNDS left over from the loop
    # above, so using them here would silently compute the SE of (0.80, 1.01).
    _se = math.sqrt(lo_rate * (1 - lo_rate) / max(half, 1)
                    + hi_rate * (1 - hi_rate) / max(n - half, 1)) * 100
    ci_lo, ci_hi = gap - 1.96 * _se, gap + 1.96 * _se
    verdict = ("inverted — ranks WORSE than chance" if ci_hi < 0 else
               "inconclusive — interval still contains zero" if ci_lo <= 0 else
               "usable signal" if gap >= 8 else "weak but real signal")
    print(f"    gap: {gap:+.1f} pts   95% CI [{ci_lo:+.1f}, {ci_hi:+.1f}]")
    print(f"    →  {verdict}")
    print(f"{'=' * w}\n")


def compare_owner_logic(db_path: str = RESEARCH_DB) -> None:
    """A/B the quant model against model + personal rules.

    Because `owner_adj` is additive and logged separately, BOTH predictions exist for the
    same row and the same outcome — so this is a paired comparison on identical data, not
    a split sample. No power is lost to splitting, and the only difference between the two
    arms is the personal adjustment itself.

    Honest-use note: define rules, THEN measure forward. Tuning rules after reading this
    output fits noise, and the result stops meaning anything.
    """
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


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "grade":
        grade_predictions()
    elif cmd in ("owner", "compare"):
        compare_owner_logic()
    else:
        report()
        compare_owner_logic()
