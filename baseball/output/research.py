"""
Model-calibration research store — DELIBERATELY SEPARATE from the betting record.

Lives in its own database file (`data/history/research.db`) and is never read by
`backtest.py`. Nothing here is a bet: these are model predictions for every batter
scored on a slate, graded against box scores so we can measure whether the model's
probabilities mean anything. No stakes, no payouts, no P&L — simulated results can
never contaminate `bets.db` or show up in the P&L summary.

Why it exists: the bet log only records the 4 legs that actually surface each day,
which is a biased sample (the ones the model already liked) and accumulates ~2 graded
rows/day. Logging all ~180 scored batters answers the real question — does a higher
model probability actually produce a higher hit rate — in days instead of months.

    python output/research.py report     # calibration + ranking-signal report
    python output/research.py grade      # grade pending predictions
"""
import sqlite3
from datetime import date, datetime, timezone

RESEARCH_DB = "data/history/research.db"


def _connect(db_path: str = RESEARCH_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT    NOT NULL,
            game_pk       INTEGER NOT NULL,
            batter_id     INTEGER NOT NULL,
            batter_name   TEXT,
            team          TEXT,
            lineup_pos    INTEGER,
            pitcher_name  TEXT,
            model_prob    REAL,            -- what the model predicted
            book_odds     INTEGER,         -- book's line, when one was posted
            book_implied  REAL,
            -- raw model inputs, so a future recalibration can be backtested properly
            h2h_ab        INTEGER,
            h2h_avg       REAL,
            recent_ab     INTEGER,
            recent_avg    REAL,
            venue_ab      INTEGER,
            venue_avg     REAL,
            team_recent_avg    REAL,
            pitcher_recent_h9  REAL,
            is_day_game   INTEGER,
            outcome       TEXT DEFAULT NULL,   -- 'win' | 'loss' | 'void'
            actual_hits   INTEGER DEFAULT NULL,
            created_at    TEXT NOT NULL,
            UNIQUE(date, game_pk, batter_id)
        );
    """)
    conn.commit()
    return conn


def log_predictions(candidates: list[dict], db_path: str = RESEARCH_DB) -> int:
    """Records every scored batter. Idempotent — re-running a slate updates in place
    rather than duplicating, so a mid-day re-run is safe."""
    if not candidates:
        return 0
    conn  = _connect(db_path)
    today = str(date.today())
    now   = datetime.now(timezone.utc).isoformat()
    n = 0
    for c in candidates:
        conn.execute("""
            INSERT INTO predictions
              (date, game_pk, batter_id, batter_name, team, lineup_pos, pitcher_name,
               model_prob, book_odds, book_implied, h2h_ab, h2h_avg, recent_ab, recent_avg,
               venue_ab, venue_avg, team_recent_avg, pitcher_recent_h9, is_day_game, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date, game_pk, batter_id) DO UPDATE SET
               model_prob=excluded.model_prob,
               book_odds=COALESCE(excluded.book_odds, predictions.book_odds),
               book_implied=COALESCE(excluded.book_implied, predictions.book_implied)
        """, (
            today, c.get("game_pk"), c.get("batter_id"), c.get("batter_name"),
            c.get("team"), c.get("lineup_pos"), c.get("pitcher_name"),
            c.get("hit_probability"), c.get("book_odds"), c.get("book_implied"),
            c.get("h2h_ab"), c.get("h2h_avg"), c.get("recent_ab"), c.get("recent_avg"),
            c.get("venue_ab"), c.get("venue_avg"), c.get("team_recent_avg"),
            c.get("pitcher_recent_h9"), int(bool(c.get("is_day_game"))), now,
        ))
        n += 1
    conn.commit()
    conn.close()
    print(f"[research] logged {n} prediction(s) for {today} (research only — not bets)")
    return n


def grade_predictions(db_path: str = RESEARCH_DB) -> int:
    """Grades pending predictions. Fetches ONE box score per game (not per batter),
    so a 180-prediction slate costs ~15 API calls rather than 180."""
    import requests
    from output.result_tracker import _game_result_state

    conn = _connect(db_path)
    pending = conn.execute(
        "SELECT id, game_pk, batter_id, batter_name FROM predictions WHERE outcome IS NULL"
    ).fetchall()
    conn.close()
    if not pending:
        print("[research] no pending predictions.")
        return 0

    by_game: dict[int, list] = {}
    for r in pending:
        by_game.setdefault(r["game_pk"], []).append(r)

    graded = 0
    for game_pk, rows in by_game.items():
        state = _game_result_state(game_pk)
        if state == "pending":
            continue
        conn = _connect(db_path)
        if state == "no_contest":
            conn.executemany("UPDATE predictions SET outcome='void' WHERE id=?",
                             [(r["id"],) for r in rows])
            conn.commit(); conn.close()
            graded += len(rows)
            continue

        try:
            resp = requests.get(
                f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore", timeout=15)
            resp.raise_for_status()
            players = {}
            for side in ("home", "away"):
                players.update(resp.json().get("teams", {}).get(side, {}).get("players", {}))
        except Exception as e:
            print(f"[research] box score fetch failed ({game_pk}): {e}")
            conn.close()
            continue

        for r in rows:
            p = players.get(f"ID{r['batter_id']}")
            hits = (p or {}).get("stats", {}).get("batting", {}).get("hits") if p else None
            if hits is None:
                continue   # did not appear (scratched / never batted)
            conn.execute("UPDATE predictions SET outcome=?, actual_hits=? WHERE id=?",
                         ("win" if int(hits) >= 1 else "loss", int(hits), r["id"]))
            graded += 1
        conn.commit()
        conn.close()

    print(f"[research] graded {graded} prediction(s).")
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
    verdict = ("model ranks better than chance" if gap > 3 else
               "NO usable ranking signal — do not bet this" if gap <= 0 else
               "inconclusive — keep collecting")
    print(f"    gap: {gap:+.1f} pts  →  {verdict}")
    print(f"{'=' * w}\n")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "grade":
        grade_predictions()
    else:
        report()
