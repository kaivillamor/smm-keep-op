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
import os
import sqlite3
from datetime import date, datetime, timezone

# Path is env-overridable so the same code runs locally (relative path, repo-local file)
# and on a container with a mounted volume (absolute path). Default preserves the
# existing local behaviour exactly — nothing changes unless RESEARCH_DB_PATH is set.
# `or` (not getenv's default arg) on purpose: a var present-but-empty — which is what
# copying .env.example verbatim produces — yields "", and getenv would hand that back
# as a real value. An empty path is never legitimate, so falling through is correct.
RESEARCH_DB = os.getenv("RESEARCH_DB_PATH") or "data/history/research.db"


def _connect(db_path: str = RESEARCH_DB) -> sqlite3.Connection:
    # A mounted volume starts empty, so the parent dir may not exist yet.
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
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
            model_prob    REAL,            -- final prediction (base_prob + owner_adj)
            base_prob     REAL,            -- quant model ONLY, before personal rules
            owner_adj     REAL,            -- the personal-logic delta, so it can be measured
            book_odds     INTEGER,         -- book's line, when one was posted
            book_implied  REAL,
            -- raw model inputs, so a future recalibration can be backtested properly
            split_avg     REAL,             -- season BA vs this pitcher's handedness
            split_ab      INTEGER,          -- AB behind that split (model gates at 30)
            h2h_ab        INTEGER,
            h2h_avg       REAL,
            recent_ab     INTEGER,
            recent_avg    REAL,
            venue_ab      INTEGER,
            venue_avg     REAL,
            team_recent_avg    REAL,
            pitcher_recent_h9  REAL,
            pitcher_season_h9  REAL,   -- model blends season/recent 60/40
            park_factor        REAL,   -- park_adj = 1 + (this - 1) * 0.3
            pitcher_days_rest  INTEGER,   -- days since the pitcher's last start
            is_day_game   INTEGER,
            outcome       TEXT DEFAULT NULL,   -- 'win' | 'loss' | 'void'
            actual_hits   INTEGER DEFAULT NULL,
            created_at    TEXT NOT NULL,
            UNIQUE(date, game_pk, batter_id)
        );
    """)
    # Migration: pitcher_days_rest was computed by hit_pipeline from the start but never
    # persisted here, so rows logged before 2026-09-08 have it NULL and always will. The
    # days-rest effect is believed to be pitcher-specific rather than league-wide (which
    # is why hit_model applies no blanket adjustment); this column is what will eventually
    # let the data settle that. Column name/type are module literals — see the note in
    # backtest.py::_ensure_schema on why identifier interpolation is unavoidable here.
    pred_cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
    for _col, _decl in (("pitcher_days_rest", "INTEGER"),
                        ("split_avg", "REAL"), ("split_ab", "INTEGER"),
                        ("base_prob", "REAL"), ("owner_adj", "REAL"),
                        ("pitcher_season_h9", "REAL"), ("park_factor", "REAL")):
        if _col not in pred_cols:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {_col} {_decl} DEFAULT NULL")
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
               model_prob, base_prob, owner_adj, book_odds, book_implied, split_avg, split_ab,
               h2h_ab, h2h_avg, recent_ab, recent_avg,
               venue_ab, venue_avg, team_recent_avg, pitcher_recent_h9,
               pitcher_season_h9, park_factor, pitcher_days_rest,
               is_day_game, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date, game_pk, batter_id) DO UPDATE SET
               model_prob=excluded.model_prob,
               book_odds=COALESCE(excluded.book_odds, predictions.book_odds),
               book_implied=COALESCE(excluded.book_implied, predictions.book_implied)
        """, (
            today, c.get("game_pk"), c.get("batter_id"), c.get("batter_name"),
            c.get("team"), c.get("lineup_pos"), c.get("pitcher_name"),
            c.get("hit_probability"), c.get("base_prob"), c.get("owner_adj"),
            c.get("book_odds"), c.get("book_implied"),
            c.get("split_avg"), c.get("split_ab"),
            c.get("h2h_ab"), c.get("h2h_avg"), c.get("recent_ab"), c.get("recent_avg"),
            c.get("venue_ab"), c.get("venue_avg"), c.get("team_recent_avg"),
            c.get("pitcher_recent_h9"), c.get("pitcher_season_h9"), c.get("park_factor"),
            c.get("pitcher_days_rest"),
            int(bool(c.get("is_day_game"))), now,
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
