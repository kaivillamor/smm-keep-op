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

        CREATE TABLE IF NOT EXISTS hit_parlays (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            parlay_num  INTEGER NOT NULL,
            leg1_id     INTEGER REFERENCES hit_legs(id),
            leg2_id     INTEGER REFERENCES hit_legs(id),
            stake       REAL    NOT NULL DEFAULT 50.0,
            outcome     TEXT    DEFAULT NULL,  -- 'win' | 'loss' | 'void'
            payout      REAL    DEFAULT NULL,  -- actual dollars returned (stake + profit)
            created_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hr_prop_candidates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT    NOT NULL,
            game_pk         INTEGER NOT NULL,
            batter_id       INTEGER NOT NULL,
            batter_name     TEXT    NOT NULL,
            team            TEXT,
            pitcher_id      INTEGER,
            barrel_rate     REAL,
            sweet_spot      REAL,
            hard_contact    REAL,
            zone_fit        REAL,
            pitcher_hr_fb   REAL,
            gate_triggered  TEXT,               -- 'barrel' | 'sweet_hc' | 'zone_hc' | 'pitcher_hrfb'
            outcome         TEXT    DEFAULT NULL,  -- 'hr' | 'no_hr'
            actual_hrs      INTEGER DEFAULT NULL,
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


def log_hr_candidates(candidates: list[dict], db_path: str = DB_PATH) -> None:
    """Persists HR prop gate candidates so result_tracker can grade them next morning.
    Skips if candidates were already logged today."""
    if not candidates:
        return
    conn  = _connect(db_path)
    today = str(date.today())

    existing = conn.execute(
        "SELECT id FROM hr_prop_candidates WHERE date=? LIMIT 1", (today,)
    ).fetchone()
    if existing:
        print(f"[backtest] HR candidates already logged for {today} — skipping.")
        conn.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        s = c.get("scores", {})
        conn.execute(
            """INSERT INTO hr_prop_candidates
               (date, game_pk, batter_id, batter_name, team, pitcher_id,
                barrel_rate, sweet_spot, hard_contact, zone_fit, pitcher_hr_fb,
                gate_triggered, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                today,
                c.get("game_pk"),
                c.get("batter_id"),
                c.get("batter_name"),
                c.get("team"),
                c.get("pitcher_id"),
                s.get("barrel_rate"),
                s.get("sweet_spot"),
                s.get("recent_hard_contact"),
                s.get("zone_fit"),
                s.get("pitcher_hr_fb"),
                s.get("gate_triggered"),
                now,
            ),
        )
    conn.commit()
    conn.close()
    print(f"[backtest] Logged {len(candidates)} HR prop candidate(s) for {today}")


def log_hit_parlay(legs: list[dict], db_path: str = DB_PATH) -> list[int]:
    """Persists hit parlay legs so result_tracker can grade them later.
    Skips insertion if already logged today. Always returns today's leg IDs in order."""
    if not legs:
        return []
    conn  = _connect(db_path)
    today = str(date.today())

    existing = conn.execute(
        "SELECT id FROM hit_legs WHERE date=? LIMIT 1", (today,)
    ).fetchone()
    if existing:
        print(f"[backtest] Hit legs already logged for {today} — skipping.")
    else:
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
        print(f"[backtest] Logged {len(legs)} hit parlay leg(s) for {today}")

    rows = conn.execute(
        "SELECT id FROM hit_legs WHERE date=? ORDER BY id", (today,)
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def log_hit_parlays(leg_ids: list[int], stake: float = 50.0,
                    db_path: str = DB_PATH) -> None:
    """Logs the 2-leg parlay structure (interleaved pairs) for a --hits-2 run.
    Requires leg_ids to already be in the DB (call log_hit_parlay first)."""
    if not leg_ids:
        return
    conn  = _connect(db_path)
    today = str(date.today())

    existing = conn.execute(
        "SELECT id FROM hit_parlays WHERE date=? LIMIT 1", (today,)
    ).fetchone()
    if existing:
        print(f"[backtest] Hit parlays already logged for {today} — skipping.")
        conn.close()
        return

    half = len(leg_ids) // 2
    now  = datetime.now(timezone.utc).isoformat()
    for i in range(half):
        conn.execute(
            """INSERT INTO hit_parlays (date, parlay_num, leg1_id, leg2_id, stake, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (today, i + 1, leg_ids[i], leg_ids[i + half], stake, now),
        )
    conn.commit()
    conn.close()
    print(f"[backtest] Logged {half} hit parlays for {today} (${stake:.0f} each)")


def record_hit_payout(date_str: str, parlay_num: int, payout_dollars: float,
                      db_path: str = DB_PATH) -> None:
    """Records the actual sportsbook payout for a winning hit parlay."""
    conn = _connect(db_path)
    conn.execute(
        "UPDATE hit_parlays SET payout=? WHERE date=? AND parlay_num=?",
        (payout_dollars, date_str, parlay_num),
    )
    conn.commit()
    conn.close()
    print(f"[backtest] Recorded ${payout_dollars:.2f} payout for hit parlay #{parlay_num} on {date_str}")


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

    parlays      = conn.execute("SELECT * FROM parlays WHERE outcome IS NOT NULL").fetchall()
    legs         = conn.execute("SELECT * FROM legs    WHERE outcome IS NOT NULL").fetchall()
    hit_legs     = conn.execute("SELECT * FROM hit_legs WHERE outcome IS NOT NULL").fetchall()
    hit_parlays  = conn.execute("SELECT * FROM hit_parlays WHERE outcome IS NOT NULL").fetchall()
    hr_cands     = conn.execute("SELECT * FROM hr_prop_candidates WHERE outcome IS NOT NULL").fetchall()
    conn.close()

    # ── Game parlays ──────────────────────────────────────────────────────────
    parlay_wins  = sum(1 for p in parlays if p["outcome"] == "win")
    parlay_total = len(parlays)
    hit_rate     = parlay_wins / parlay_total if parlay_total else 0

    gp_staked    = parlay_total * 10.0
    gp_returned  = sum((p["payout"] or 0) * 10.0 for p in parlays if p["outcome"] == "win")
    gp_net       = gp_returned - gp_staked
    roi          = gp_net / gp_staked if gp_staked else 0

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

    # ── Hit parlays (2-leg pairs) ─────────────────────────────────────────────
    hp_resolved  = [p for p in hit_parlays if p["outcome"] in ("win", "loss", "void")]
    hp_wins      = sum(1 for p in hp_resolved if p["outcome"] == "win")
    hp_losses    = sum(1 for p in hp_resolved if p["outcome"] == "loss")
    hp_staked    = sum(p["stake"] for p in hp_resolved if p["outcome"] in ("win", "loss"))
    hp_returned  = sum(p["payout"] for p in hp_resolved
                       if p["outcome"] == "win" and p["payout"] is not None)
    hp_wins_no_payout = sum(1 for p in hp_resolved
                            if p["outcome"] == "win" and p["payout"] is None)
    hp_net       = (hp_returned - hp_staked) if hp_staked and not hp_wins_no_payout else None

    # ── HR prop candidates ────────────────────────────────────────────────────
    hr_hits  = sum(1 for r in hr_cands if r["outcome"] == "hr")
    hr_rate  = round(hr_hits / len(hr_cands), 4) if hr_cands else None

    # ── Combined net (only where all payouts are known) ───────────────────────
    combined_net = gp_net + (hp_net if hp_net is not None else 0)
    combined_complete = hp_net is not None  # False = hit parlay payouts still TBD

    summary = {
        # game parlays
        "parlays_tracked":      parlay_total,
        "parlay_hit_rate":      round(hit_rate, 4),
        "gp_net":               round(gp_net, 2),
        "roi":                  round(roi, 4),
        "avg_clv":              round(avg_clv, 4) if avg_clv is not None else None,
        "ml_win_rate":          _win_rate(ml_legs),
        "ml_legs_graded":       len([l for l in ml_legs if l["outcome"] in ("win", "loss")]),
        "total_win_rate":       _win_rate(total_legs),
        "total_legs_graded":    len([l for l in total_legs if l["outcome"] in ("win", "loss")]),
        # hit parlay legs
        "hit_legs_graded":      len(hit_resolved),
        "hit_leg_win_rate":     round(hit_rate_leg, 4) if hit_rate_leg is not None else None,
        # hit parlays
        "hp_tracked":           len(hp_resolved),
        "hp_wins":              hp_wins,
        "hp_losses":            hp_losses,
        "hp_staked":            round(hp_staked, 2),
        "hp_returned":          round(hp_returned, 2),
        "hp_net":               round(hp_net, 2) if hp_net is not None else None,
        "hp_wins_no_payout":    hp_wins_no_payout,
        # hr props
        "hr_cands_graded":      len(hr_cands),
        "hr_hit_rate":          hr_rate,
        # combined
        "combined_net":         round(combined_net, 2),
        "combined_complete":    combined_complete,
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
    w = 40
    print(f"\n{'=' * w}")

    # ── Combined P&L ─────────────────────────────────────────────────────────
    net = s["combined_net"]
    net_str = f"${net:+.2f}" if s["combined_complete"] else f"${s['gp_net']:+.2f} + hit TBD"
    print(f"  NET P&L (all bets):  {net_str}")
    print(f"{'─' * w}")

    # ── Game parlays ──────────────────────────────────────────────────────────
    print(f"  GAME PARLAYS  ({s['parlays_tracked']} tracked, $10/bet)")
    print(f"{'─' * w}")
    print(f"  Hit rate:   {s['parlay_hit_rate'] * 100:.1f}%")
    print(f"  Net P&L:    ${s['gp_net']:+.2f}  ({s['roi']*100:+.1f}% ROI)")
    if s["avg_clv"] is not None:
        print(f"  Avg CLV:    {s['avg_clv'] * 100:+.2f}%")
    if s["ml_win_rate"] is not None:
        print(f"  ML legs:    {s['ml_win_rate'] * 100:.1f}%  ({s['ml_legs_graded']} graded)")
    if s["total_win_rate"] is not None:
        print(f"  Totals:     {s['total_win_rate'] * 100:.1f}%  ({s['total_legs_graded']} graded)")

    # ── Hit parlays ───────────────────────────────────────────────────────────
    print(f"{'─' * w}")
    print(f"  HIT PARLAYS  ({s['hp_tracked']} tracked, $50/bet)")
    print(f"{'─' * w}")
    if s["hp_tracked"]:
        print(f"  Won/Lost:   {s['hp_wins']}/{s['hp_losses']}  "
              f"({s['hp_wins'] / max(s['hp_wins'] + s['hp_losses'], 1) * 100:.0f}%)")
        print(f"  Staked:     ${s['hp_staked']:.2f}")
        if s["hp_net"] is not None:
            print(f"  Net P&L:    ${s['hp_net']:+.2f}")
        else:
            returned_str = f"${s['hp_returned']:.2f} confirmed" if s["hp_returned"] else "—"
            print(f"  Returned:   {returned_str}")
            print(f"  Note: {s['hp_wins_no_payout']} win(s) need payout — "
                  f"use --record-hit-win DATE NUM AMOUNT")
    if s["hit_leg_win_rate"] is not None:
        print(f"  Leg rate:   {s['hit_leg_win_rate'] * 100:.1f}%  ({s['hit_legs_graded']} legs)")

    # ── HR props ──────────────────────────────────────────────────────────────
    print(f"{'─' * w}")
    print(f"  HR PROPS  ({s['hr_cands_graded']} graded)")
    print(f"{'─' * w}")
    if s["hr_hit_rate"] is not None:
        print(f"  HR rate:    {s['hr_hit_rate'] * 100:.1f}%  ({s['hr_cands_graded']} candidates)")
    else:
        print(f"  HR rate:    — (no resolved candidates yet)")
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
