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
from datetime import date, datetime, timezone

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
            date          TEXT    NOT NULL,
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
            -- model inputs, stored so a later refit can be run properly
            season_avg    REAL,                  -- season average for this market
            recent_avg    REAL,                  -- last-N-games average
            recent_games  INTEGER,
            minutes_avg   REAL,                  -- the single biggest NBA prop driver
            vs_opp_avg    REAL,                  -- this market vs this opponent
            vs_opp_games  INTEGER,
            usage_proxy   REAL,                  -- FGA + 0.44*FTA + TOV (ESPN lacks true USG%)
            days_rest     INTEGER,               -- back-to-backs matter far more than in MLB
            is_home       INTEGER,
            opp_def_rating REAL,
            outcome       TEXT DEFAULT NULL,      -- 'win' | 'loss' | 'void'
            actual_value  REAL DEFAULT NULL,      -- what the player actually recorded
            created_at    TEXT NOT NULL,
            UNIQUE(date, game_id, player_id, market)
        );
    """)
    conn.commit()
    return conn


_FIELDS = ("game_id", "player_id", "player_name", "team", "opponent", "market", "line",
           "model_prob", "base_prob", "owner_adj", "book_odds", "book_implied",
           "season_avg", "recent_avg", "recent_games", "minutes_avg", "vs_opp_avg",
           "vs_opp_games", "usage_proxy", "days_rest", "is_home", "opp_def_rating")


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
    today = str(date.today())
    now = datetime.now(timezone.utc).isoformat()
    cols = ", ".join(("date",) + _FIELDS + ("created_at",))
    marks = ", ".join("?" * (len(_FIELDS) + 2))
    sql = (f"INSERT INTO predictions ({cols}) VALUES ({marks}) "
           "ON CONFLICT(date, game_id, player_id, market) DO UPDATE SET "
           "  model_prob=excluded.model_prob, base_prob=excluded.base_prob, "
           "  owner_adj=excluded.owner_adj, "
           "  line=COALESCE(excluded.line, predictions.line), "
           "  book_odds=COALESCE(excluded.book_odds, predictions.book_odds), "
           "  book_implied=COALESCE(excluded.book_implied, predictions.book_implied)")
    n = 0
    for c in candidates:
        conn.execute(sql, (today, *(c.get(f) for f in _FIELDS), now))
        n += 1
    conn.commit()
    conn.close()
    print(f"[research] logged {n} prediction(s) for {today} (research only — not bets)")
    return n


def grade_predictions(db_path: str = RESEARCH_DB) -> int:
    """TODO — needs the ESPN box-score fetcher, which is not built yet.

    Shape of the work (mirrors the baseball version, which proved these points the hard
    way):
      * fetch ONE box score per game, not one per player
      * only grade FINAL games — box scores populate live, and baseball graded a player
        'loss' before his game started because of this
      * treat postponed/suspended as 'void', never 'loss'
      * outcome = 'win' if actual_value > line else 'loss'; a push (value == line) is
        'void', which has no baseball analogue since Over-0.5 lines cannot push
    """
    raise NotImplementedError(
        "ESPN box-score grading not built. See basketball/pipeline/stats_fetcher.py.")


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
