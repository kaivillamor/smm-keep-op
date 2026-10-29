"""
Model-calibration research store: predictions for every scored batter, graded against box
scores, in its own database (research.db). Nothing here is a bet, and backtest.py never
reads it.

    python output/research.py report     # calibration + ranking-signal report
    python output/research.py grade      # grade pending predictions
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

# As a script only output/ is on sys.path, so add the package root before the imports.
if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.stats_fetcher import POSTSEASON_START, season_phase, slate_date

# `or`, not a getenv default: an empty RESEARCH_DB_PATH must fall back too.
RESEARCH_DB = os.getenv("RESEARCH_DB_PATH") or "data/history/research.db"

# Rows that count as evidence: everything but the postseason. `IS NOT`, not `!=`, keeps
# NULL-phase rows. web/app.py imports this.
EVIDENCE_SQL = "season_phase IS NOT 'postseason'"

# When rows started keying on the game's slate; rows written before carry the collection date.
_SLATE_KEY_FIX = "2026-09-30T10:33"


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
            date          TEXT    NOT NULL,   -- the game's Eastern slate date, from first
                                              --  pitch (created_at is collection time)
            game_pk       INTEGER NOT NULL,
            batter_id     INTEGER NOT NULL,
            batter_name   TEXT,
            team          TEXT,
            lineup_pos    INTEGER,
            pitcher_name  TEXT,
            model_prob    REAL,            -- final prediction (base_prob + owner_adj)
            calibrated_prob REAL,          -- model_prob shrunk toward the base rate; what
                                           --  the per-leg EV gate compares with the price
            base_prob     REAL,            -- quant model only, before owner rules
            owner_adj     REAL,            -- the owner-rules delta
            book_odds     INTEGER,         -- book's line, when one was posted
            book_implied  REAL,
            book          TEXT,            -- which sportsbook priced it
            -- raw model inputs
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
            pitcher_season_h9  REAL,   -- shrunk by pitcher_season_ip on rows with
                                       --  calibrated_prob; raw, floored at 0.5, before
            pitcher_season_ip  REAL,   -- innings behind it: with w = ip/(ip+H9_IP_PRIOR),
                                       --  raw = (h9 - (1-w)*LEAGUE_H_PER_9) / w
            park_factor        REAL,   -- park_adj = 1 + (this - 1) * 0.3
            pitcher_days_rest  INTEGER,   -- days since the pitcher's last start
            is_day_game   INTEGER,
            outcome       TEXT DEFAULT NULL,   -- 'win' | 'loss' | 'void'
            actual_hits   INTEGER DEFAULT NULL,
            season_phase  TEXT DEFAULT NULL,  -- 'regular' | 'postseason' | NULL=unknown
            created_at    TEXT NOT NULL,
            UNIQUE(date, game_pk, batter_id)
        );
    """)
    # Migrations. Column names and types are literals here: SQLite can't bind identifiers.
    pred_cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
    for _col, _decl in (("pitcher_days_rest", "INTEGER"),
                        ("split_avg", "REAL"), ("split_ab", "INTEGER"),
                        ("base_prob", "REAL"), ("owner_adj", "REAL"),
                        ("pitcher_season_h9", "REAL"), ("park_factor", "REAL"),
                        ("book", "TEXT"), ("calibrated_prob", "REAL"),
                        ("season_phase", "TEXT"), ("pitcher_season_ip", "REAL")):
        if _col not in pred_cols:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {_col} {_decl} DEFAULT NULL")
    # Price provenance, bounded on created_at (the de-dup below rewrites `date`): a price
    # with no book is 'fanduel_feed_suspect' before the feed fix, 'unattributed' after it.
    conn.execute(
        "UPDATE predictions SET book = 'fanduel_feed_suspect' "
        "WHERE book_odds IS NOT NULL AND book IS NULL "
        "AND created_at < '2026-09-17T19:00'")
    conn.execute(
        "UPDATE predictions SET book = 'unattributed' "
        "WHERE book_odds IS NOT NULL AND (book IS NULL OR book = 'fanduel_feed_suspect') "
        "AND created_at >= '2026-09-17T19:00'")

    # ── De-duplicate on the natural key (idempotent) ─────────────────────────────
    # One row per (game_pk, batter_id): the last logged survives. Before _SLATE_KEY_FIX it
    # takes the group's earliest date (the real slate); after, two dates mean a reschedule
    # and it keeps its own. Losers are archived as JSON, never dropped.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions_superseded (
            id          INTEGER PRIMARY KEY,   -- original predictions.id
            game_pk     INTEGER,
            batter_id   INTEGER,
            date        TEXT,
            created_at  TEXT,
            archived_at TEXT NOT NULL,
            reason      TEXT NOT NULL,
            row_json    TEXT NOT NULL
        );
    """)
    dupes = conn.execute("""
        SELECT game_pk, batter_id, MIN(date) AS slate, MAX(id) AS keep_id, COUNT(*) AS n,
               MAX(created_at) AS last_written
        FROM predictions
        GROUP BY game_pk, batter_id
        HAVING n > 1
    """).fetchall()
    if dupes:
        import json as _json
        _now = datetime.now(timezone.utc).isoformat()
        _archived: dict[str, int] = {}
        for _d in dupes:
            pre_fix = _d["last_written"] < _SLATE_KEY_FIX
            reason = ("collection-date duplicate (pre-2026-09-30)" if pre_fix
                      else "rescheduled game — superseded by the makeup-date row")
            losers = conn.execute(
                "SELECT * FROM predictions WHERE game_pk=? AND batter_id=? AND id<>?",
                (_d["game_pk"], _d["batter_id"], _d["keep_id"])).fetchall()
            for _row in losers:
                conn.execute(
                    "INSERT OR IGNORE INTO predictions_superseded "
                    "(id, game_pk, batter_id, date, created_at, archived_at, reason, row_json) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (_row["id"], _row["game_pk"], _row["batter_id"], _row["date"],
                     _row["created_at"], _now, reason,
                     _json.dumps({k: _row[k] for k in _row.keys()})))
                _archived[reason] = _archived.get(reason, 0) + 1
            # DELETE first: re-dating the survivor would collide with a loser's unique key.
            conn.execute(
                "DELETE FROM predictions WHERE game_pk=? AND batter_id=? AND id<>?",
                (_d["game_pk"], _d["batter_id"], _d["keep_id"]))
            if pre_fix:
                conn.execute("UPDATE predictions SET date=? WHERE id=?",
                             (_d["slate"], _d["keep_id"]))
        for reason, n in _archived.items():
            print(f"[research] archived {n} row(s) to predictions_superseded: {reason}",
                  flush=True)

    # Pre-fix games split across two dates move to the earliest; bounded on created_at.
    split = conn.execute("""
        SELECT game_pk, MIN(date) AS slate FROM predictions
        WHERE created_at < ?
        GROUP BY game_pk HAVING COUNT(DISTINCT date) > 1
    """, (_SLATE_KEY_FIX,)).fetchall()
    for _g in split:
        conn.execute("UPDATE predictions SET date=? "
                     "WHERE game_pk=? AND created_at < ? AND date <> ?",
                     (_g["slate"], _g["game_pk"], _SLATE_KEY_FIX, _g["slate"]))
    if split:
        print(f"[research] re-dated {len(split)} pre-fix game(s) split across two slates",
              flush=True)

    # Backfill season_phase for every row that predates the column.
    conn.execute("UPDATE predictions SET season_phase = CASE "
                 "WHEN date >= ? THEN 'postseason' ELSE 'regular' END "
                 "WHERE season_phase IS NULL AND date LIKE '2026-%'",
                 (POSTSEASON_START[2026],))
    conn.commit()
    return conn


def log_predictions(candidates: list[dict], db_path: str = RESEARCH_DB) -> int:
    """Record every scored batter, keyed by the game's slate date. Idempotent: a re-run
    updates rows in place."""
    if not candidates:
        return 0
    conn  = _connect(db_path)
    default_date = slate_date()   # only for a candidate carrying no game_date of its own
    now   = datetime.now(timezone.utc).isoformat()
    dates_seen: set[str] = set()
    n = 0
    for c in candidates:
        game_date = c.get("game_date") or default_date
        dates_seen.add(game_date)
        conn.execute("""
            INSERT INTO predictions
              (date, game_pk, batter_id, batter_name, team, lineup_pos, pitcher_name,
               model_prob, calibrated_prob, base_prob, owner_adj,
               book_odds, book_implied, book,
               split_avg, split_ab,
               h2h_ab, h2h_avg, recent_ab, recent_avg,
               venue_ab, venue_avg, team_recent_avg, pitcher_recent_h9,
               pitcher_season_h9, pitcher_season_ip, park_factor, pitcher_days_rest,
               is_day_game, season_phase, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date, game_pk, batter_id) DO UPDATE SET
               model_prob=excluded.model_prob,
               calibrated_prob=excluded.calibrated_prob,
               book_odds=COALESCE(excluded.book_odds, predictions.book_odds),
               book_implied=COALESCE(excluded.book_implied, predictions.book_implied),
               -- `book` must refresh with the odds it describes.
               book=COALESCE(excluded.book, predictions.book)
        """, (
            game_date, c.get("game_pk"), c.get("batter_id"), c.get("batter_name"),
            c.get("team"), c.get("lineup_pos"), c.get("pitcher_name"),
            c.get("hit_probability"), c.get("calibrated_prob"),
            c.get("base_prob"), c.get("owner_adj"),
            c.get("book_odds"), c.get("book_implied"), c.get("book"),
            c.get("split_avg"), c.get("split_ab"),
            c.get("h2h_ab"), c.get("h2h_avg"), c.get("recent_ab"), c.get("recent_avg"),
            c.get("venue_ab"), c.get("venue_avg"), c.get("team_recent_avg"),
            c.get("pitcher_recent_h9"), c.get("pitcher_season_h9"),
            c.get("pitcher_season_ip"), c.get("park_factor"),
            c.get("pitcher_days_rest"),
            int(bool(c.get("is_day_game"))), season_phase(game_date), now,
        ))
        n += 1
    conn.commit()
    conn.close()
    slates = ", ".join(sorted(dates_seen)) or default_date
    print(f"[research] logged {n} prediction(s) for {slates} (research only — not bets)")
    return n


def grade_predictions(db_path: str = RESEARCH_DB) -> int:
    """Grade pending predictions: one box score per game, one connection per run, and a
    commit per game so a crash keeps what was graded."""
    import requests
    from output.result_tracker import _game_result_state

    conn = _connect(db_path)
    try:
        pending = conn.execute(
            "SELECT id, date, game_pk, batter_id, batter_name FROM predictions "
            "WHERE outcome IS NULL"
        ).fetchall()
        if not pending:
            print("[research] no pending predictions.")
            return 0

        # Keyed by slate too: a postponed date's rows void and the makeup's grade.
        by_game: dict[tuple[int, str], list] = {}
        for r in pending:
            by_game.setdefault((r["game_pk"], r["date"]), []).append(r)

        graded = voided_dnp = 0
        for (game_pk, slate), rows in by_game.items():
            state = _game_result_state(game_pk, slate)
            if state == "pending":
                continue
            if state == "no_contest":
                conn.executemany("UPDATE predictions SET outcome='void' WHERE id=?",
                                 [(r["id"],) for r in rows])
                conn.commit()
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
                continue

            for r in rows:
                p = players.get(f"ID{r['batter_id']}")
                hits = (p or {}).get("stats", {}).get("batting", {}).get("hits") if p else None
                if hits is not None:
                    conn.execute("UPDATE predictions SET outcome=?, actual_hits=? WHERE id=?",
                                 ("win" if int(hits) >= 1 else "loss", int(hits), r["id"]))
                    graded += 1
                elif players:
                    # Populated box, no batting line: he never batted, so void. An empty
                    # box score stays pending.
                    conn.execute("UPDATE predictions SET outcome='void' WHERE id=?", (r["id"],))
                    graded += 1
                    voided_dnp += 1
            conn.commit()
    finally:
        conn.close()

    print(f"[research] graded {graded} prediction(s)"
          + (f", {voided_dnp} void as did-not-bat" if voided_dnp else "") + ".")
    return graded


def report(db_path: str = RESEARCH_DB) -> None:
    """Calibration and ranking-signal report on the evidence rows (EVIDENCE_SQL)."""
    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT model_prob p, outcome FROM predictions "
        f"WHERE outcome IN ('win','loss') AND model_prob IS NOT NULL AND {EVIDENCE_SQL}")]
    total = conn.execute(
        f"SELECT COUNT(*) c FROM predictions WHERE {EVIDENCE_SQL}").fetchone()["c"]
    apart = conn.execute(
        f"SELECT COUNT(*) c FROM predictions WHERE NOT ({EVIDENCE_SQL})").fetchone()["c"]
    unknown = conn.execute(
        "SELECT COUNT(*) c FROM predictions WHERE season_phase IS NULL").fetchone()["c"]
    conn.close()

    w = 56
    print(f"\n{'=' * w}\n  MODEL CALIBRATION (research only — not bets)\n{'=' * w}")
    print(f"  predictions logged : {total}")
    print(f"  graded             : {len(rows)}")
    if apart:
        print(f"  postseason, apart  : {apart}  (a different regime, not evidence — §29)")
    if unknown:
        print(f"  phase unknown      : {unknown}  (counted in: set POSTSEASON_START for its season)")
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
    import math
    # lo_rate/hi_rate, not lo/hi: those are the bucket bounds left over from the loop above.
    _se = math.sqrt(lo_rate * (1 - lo_rate) / max(half, 1)
                    + hi_rate * (1 - hi_rate) / max(n - half, 1)) * 100
    ci_lo, ci_hi = gap - 1.96 * _se, gap + 1.96 * _se
    # "Usable" reads the CI's lower bound. Twin: USABLE_GAP in web/frontend/src/App.jsx.
    usable_gap = 8.0
    verdict = ("inverted — ranks WORSE than chance" if ci_hi < 0 else
               "inconclusive — interval still contains zero" if ci_lo <= 0 else
               "weak but real signal" if ci_lo < usable_gap else "usable signal")
    print(f"    gap: {gap:+.1f} pts   95% CI [{ci_lo:+.1f}, {ci_hi:+.1f}]")
    print(f"    →  {verdict}")
    print(f"{'=' * w}\n")


def compare_owner_logic(db_path: str = RESEARCH_DB) -> None:
    """A/B the quant model (base_prob) against model + owner rules (model_prob), paired on
    the same graded rows."""
    import math

    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT model_prob, base_prob, owner_adj, outcome FROM predictions "
        "WHERE outcome IN ('win','loss') AND base_prob IS NOT NULL "
        f"  AND model_prob IS NOT NULL AND {EVIDENCE_SQL}")]
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
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "grade":
        grade_predictions()
    elif cmd in ("owner", "compare"):
        compare_owner_logic()
    else:
        report()
        compare_owner_logic()
