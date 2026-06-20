import json
import sqlite3
from datetime import date, datetime, timezone

DB_PATH = "data/history/bets.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS parlays (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT    NOT NULL,
            num_legs      INTEGER NOT NULL,
            combined_odds INTEGER NOT NULL,
            total_edge    REAL    NOT NULL,
            outcome       TEXT    DEFAULT NULL,  -- 'win' | 'loss' | 'push'
            payout        REAL    DEFAULT NULL,  -- actual payout multiplier
            created_at    TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS legs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            parlay_id     INTEGER NOT NULL REFERENCES parlays(id),
            game_id       TEXT,
            home_team     TEXT,
            away_team     TEXT,
            bet_type      TEXT,               -- 'ml' | 'total'
            side          TEXT,               -- 'home'|'away'|'over'|'under'
            team          TEXT,
            display       TEXT,
            edge          REAL,
            odds          INTEGER,
            book          TEXT,
            line          REAL,
            llm_downgraded INTEGER DEFAULT 0,
            llm_reason    TEXT,
            outcome       TEXT    DEFAULT NULL,  -- 'win' | 'loss' | 'push'
            closing_odds  INTEGER DEFAULT NULL,  -- for CLV calculation
            created_at    TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hit_legs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT    NOT NULL,
            game_pk         INTEGER NOT NULL,
            batter_id       INTEGER NOT NULL,
            batter_name     TEXT    NOT NULL,
            team            TEXT,
            opponent_team   TEXT,
            pitcher_name    TEXT,
            lineup_pos      INTEGER,
            hit_probability REAL,
            outcome         TEXT    DEFAULT NULL,  -- 'win' | 'loss'
            actual_hits     INTEGER DEFAULT NULL,
            created_at      TEXT    NOT NULL
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_parlay(parlay: dict, db_path: str = DB_PATH) -> int:
    """
    Saves a generated parlay and its legs to the database.
    Returns the parlay ID for future outcome updates.
    Skips logging if a parlay was already saved today (prevents duplicate runs).
    """
    conn  = _connect(db_path)
    today = str(date.today())

    existing = conn.execute(
        "SELECT id FROM parlays WHERE date=? LIMIT 1", (today,)
    ).fetchone()
    if existing:
        print(f"[backtest] Parlay already logged for {today} (id={existing['id']}) — skipping.")
        conn.close()
        return existing["id"]

    now = datetime.now(timezone.utc).isoformat()

    cur = conn.execute(
        """INSERT INTO parlays (date, num_legs, combined_odds, total_edge, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (today, parlay["num_legs"], parlay["combined_odds"], parlay["total_edge"], now),
    )
    parlay_id = cur.lastrowid

    for leg in parlay.get("legs", []):
        conn.execute(
            """INSERT INTO legs
               (parlay_id, game_id, home_team, away_team, bet_type, side, team,
                display, edge, odds, book, line, llm_downgraded, llm_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                parlay_id,
                leg.get("game_id"),
                leg.get("home_team"),
                leg.get("away_team"),
                leg.get("bet_type"),
                leg.get("side"),
                leg.get("team"),
                leg.get("display"),
                leg.get("edge"),
                leg.get("odds"),
                leg.get("book"),
                leg.get("line"),
                int(leg.get("llm_downgraded", False)),
                leg.get("llm_reason"),
                now,
            ),
        )

    conn.commit()
    conn.close()
    print(f"[backtest] Logged parlay #{parlay_id} ({parlay['num_legs']} legs, {_fmt_odds(parlay['combined_odds'])})")
    return parlay_id


def log_hit_parlay(legs: list[dict], db_path: str = DB_PATH) -> None:
    """Persists hit parlay legs so result_tracker can grade them later.
    Skips if hit legs were already logged today."""
    if not legs:
        return
    conn  = _connect(db_path)
    today = str(date.today())

    existing = conn.execute(
        "SELECT id FROM hit_legs WHERE date=? LIMIT 1", (today,)
    ).fetchone()
    if existing:
        print(f"[backtest] Hit legs already logged for {today} — skipping.")
        conn.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    for leg in legs:
        conn.execute(
            """INSERT INTO hit_legs
               (date, game_pk, batter_id, batter_name, team, opponent_team,
                pitcher_name, lineup_pos, hit_probability, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                today,
                leg.get("game_pk"),
                leg.get("batter_id"),
                leg.get("batter_name"),
                leg.get("team"),
                leg.get("opponent_team"),
                leg.get("pitcher_name"),
                leg.get("lineup_pos"),
                leg.get("hit_probability"),
                now,
            ),
        )
    conn.commit()
    conn.close()
    print(f"[backtest] Logged {len(legs)} hit parlay leg(s) for {today}")


# ---------------------------------------------------------------------------
# Outcome updates (run after games resolve)
# ---------------------------------------------------------------------------

def update_parlay_outcome(parlay_id: int, outcome: str, payout: float = None,
                          db_path: str = DB_PATH) -> None:
    """outcome: 'win' | 'loss' | 'push'"""
    conn = _connect(db_path)
    conn.execute(
        "UPDATE parlays SET outcome=?, payout=? WHERE id=?",
        (outcome, payout, parlay_id),
    )
    conn.commit()
    conn.close()


def update_leg_outcome(leg_id: int, outcome: str, closing_odds: int = None,
                       db_path: str = DB_PATH) -> None:
    """
    Records the result of a single leg and optionally the closing line.
    closing_odds enables CLV tracking — ideally pulled from The Odds API
    just before first pitch.
    """
    conn = _connect(db_path)
    conn.execute(
        "UPDATE legs SET outcome=?, closing_odds=? WHERE id=?",
        (outcome, closing_odds, leg_id),
    )
    conn.commit()
    conn.close()


def get_pending_parlays(db_path: str = DB_PATH) -> list[dict]:
    """Returns all parlays without a recorded outcome yet."""
    conn  = _connect(db_path)
    rows  = conn.execute(
        "SELECT * FROM parlays WHERE outcome IS NULL ORDER BY date DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Performance evaluation
# ---------------------------------------------------------------------------

def evaluate_results(db_path: str = DB_PATH) -> dict:
    conn = _connect(db_path)

    parlays   = conn.execute("SELECT * FROM parlays WHERE outcome IS NOT NULL").fetchall()
    legs      = conn.execute("SELECT * FROM legs    WHERE outcome IS NOT NULL").fetchall()
    hit_legs  = conn.execute("SELECT * FROM hit_legs WHERE outcome IS NOT NULL").fetchall()
    conn.close()

    # ── Game parlays ──────────────────────────────────────────────────────────
    game_parlays  = [p for p in parlays]
    parlay_wins   = sum(1 for p in game_parlays if p["outcome"] == "win")
    parlay_total  = len(game_parlays)
    hit_rate      = parlay_wins / parlay_total if parlay_total else 0

    total_staked  = parlay_total
    total_return  = sum((p["payout"] or 0) for p in game_parlays if p["outcome"] == "win")
    roi           = (total_return - total_staked) / total_staked if total_staked else 0

    clv_legs = [l for l in legs if l["closing_odds"] is not None]
    avg_clv  = (
        sum(closing_line_value(dict(l)) for l in clv_legs) / len(clv_legs)
        if clv_legs else None
    )

    ml_legs    = [l for l in legs if l["bet_type"] == "ml"]
    total_legs = [l for l in legs if l["bet_type"] == "total"]

    # ── Hit parlay legs ───────────────────────────────────────────────────────
    hit_resolved = [l for l in hit_legs if l["outcome"] in ("win", "loss")]
    hit_wins     = sum(1 for l in hit_resolved if l["outcome"] == "win")
    hit_rate_leg = hit_wins / len(hit_resolved) if hit_resolved else None

    summary = {
        # game parlays
        "parlays_tracked":   parlay_total,
        "parlay_hit_rate":   round(hit_rate, 4),
        "roi":               round(roi, 4),
        "avg_clv":           round(avg_clv, 4) if avg_clv is not None else None,
        "ml_win_rate":       _win_rate(ml_legs),
        "ml_legs_graded":    len([l for l in ml_legs if l["outcome"] in ("win", "loss")]),
        "total_win_rate":    _win_rate(total_legs),
        "total_legs_graded": len([l for l in total_legs if l["outcome"] in ("win", "loss")]),
        # hit parlay
        "hit_legs_graded":   len(hit_resolved),
        "hit_leg_win_rate":  round(hit_rate_leg, 4) if hit_rate_leg is not None else None,
    }

    _print_summary(summary)
    return summary


def closing_line_value(leg: dict) -> float:
    """
    CLV = implied probability at our odds - implied probability at closing odds.
    Positive CLV means we got better value than the closing market offered.
    """
    our_odds     = leg.get("odds")
    closing_odds = leg.get("closing_odds")
    if not our_odds or not closing_odds:
        return 0.0
    return _implied_prob(our_odds) - _implied_prob(closing_odds)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _win_rate(legs: list) -> float | None:
    resolved = [l for l in legs if l["outcome"] in ("win", "loss")]
    if not resolved:
        return None
    return round(sum(1 for l in resolved if l["outcome"] == "win") / len(resolved), 4)


def _count_llm_actions(db_path: str) -> dict:
    conn     = _connect(db_path)
    removed  = conn.execute("SELECT COUNT(*) FROM legs WHERE llm_downgraded=1 AND outcome IS NULL").fetchone()[0]
    downgraded = conn.execute("SELECT COUNT(*) FROM legs WHERE llm_downgraded=1").fetchone()[0]
    conn.close()
    return {"downgraded": downgraded}


def _implied_prob(american_odds: int) -> float:
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def _fmt_odds(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def _print_summary(s: dict) -> None:
    w = 36
    print(f"\n{'=' * w}")
    print(f"  GAME PARLAYS  ({s['parlays_tracked']} tracked)")
    print(f"{'─' * w}")
    print(f"  Hit rate:      {s['parlay_hit_rate'] * 100:.1f}%")
    print(f"  ROI ($10/bet): ${(s['roi'] * 10 * s['parlays_tracked']):.2f}  ({s['roi']*100:+.1f}%)")
    if s["avg_clv"] is not None:
        print(f"  Avg CLV:       {s['avg_clv'] * 100:+.2f}%")
    if s["ml_win_rate"] is not None:
        print(f"  ML legs:       {s['ml_win_rate'] * 100:.1f}%  ({s['ml_legs_graded']} graded)")
    if s["total_win_rate"] is not None:
        print(f"  Total legs:    {s['total_win_rate'] * 100:.1f}%  ({s['total_legs_graded']} graded)")

    print(f"{'─' * w}")
    print(f"  HIT PARLAY LEGS  ({s['hit_legs_graded']} graded)")
    print(f"{'─' * w}")
    if s["hit_leg_win_rate"] is not None:
        print(f"  Leg hit rate:  {s['hit_leg_win_rate'] * 100:.1f}%  ({s['hit_legs_graded']} legs)")
    else:
        print(f"  Leg hit rate:  — (no resolved legs yet)")

    print(f"{'─' * w}")
    print(f"  HR PROPS         (not yet tracked)")
    print(f"{'=' * w}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        evaluate_results()
    else:
        pending = get_pending_parlays()
        if not pending:
            print("No pending parlays.")
        else:
            print(f"\n{len(pending)} pending parlay(s) awaiting outcomes:\n")
            for p in pending:
                print(f"  #{p['id']} — {p['date']} | {p['num_legs']} legs | {_fmt_odds(p['combined_odds'])}")
