import json
import sqlite3
from datetime import date, datetime, timezone

# NOTE: parlay_builder is imported lazily inside the logging functions below, not
# here at module top. Running `python output/backtest.py summary` executes this
# file as a script (only 'output/' on sys.path), where 'parlay' isn't importable;
# summary never logs, so the import only needs to exist when main.py does the logging.

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
            book_odds       INTEGER DEFAULT NULL,  -- 1+ hit American odds from the prop-odds API
            book_implied    REAL    DEFAULT NULL,  -- book's implied prob (vig included)
            ev              REAL    DEFAULT NULL,  -- our prob − book implied (edge)
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
            stake       REAL    NOT NULL DEFAULT 10.0,
            odds        INTEGER DEFAULT NULL,  -- combined American odds from the bet slip
            outcome     TEXT    DEFAULT NULL,  -- 'win' | 'loss' | 'void'
            payout      REAL    DEFAULT NULL,  -- PROFIT/winnings only (total received = stake + payout)
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
            book_odds       INTEGER DEFAULT NULL,  -- 1+ HR American odds from the prop-odds API
            book_implied    REAL    DEFAULT NULL,  -- book's implied prob (vig included)
            outcome         TEXT    DEFAULT NULL,  -- 'hr' | 'no_hr'
            actual_hrs      INTEGER DEFAULT NULL,
            created_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hr_parlays (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            parlay_num  INTEGER NOT NULL,
            leg1_id     INTEGER REFERENCES hr_prop_candidates(id),
            leg2_id     INTEGER REFERENCES hr_prop_candidates(id),
            stake       REAL    NOT NULL DEFAULT 10.0,
            odds        INTEGER DEFAULT NULL,  -- combined American odds (both to hit 1+ HR)
            outcome     TEXT    DEFAULT NULL,  -- 'win' | 'loss'
            payout      REAL    DEFAULT NULL,  -- PROFIT/winnings only (total received = stake + payout)
            created_at  TEXT    NOT NULL
        );
    """)
    # Migration: existing DBs created before the odds column was added
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hit_parlays)").fetchall()}
    if "odds" not in cols:
        conn.execute("ALTER TABLE hit_parlays ADD COLUMN odds INTEGER DEFAULT NULL")
    # Migration: hit_legs gained book odds / implied / ev columns
    leg_cols = {r[1] for r in conn.execute("PRAGMA table_info(hit_legs)").fetchall()}
    for col, decl in (("book_odds", "INTEGER"), ("book_implied", "REAL"), ("ev", "REAL")):
        if col not in leg_cols:
            conn.execute(f"ALTER TABLE hit_legs ADD COLUMN {col} {decl} DEFAULT NULL")
    # Migration: hr_prop_candidates gained book odds / implied columns
    hr_cols = {r[1] for r in conn.execute("PRAGMA table_info(hr_prop_candidates)").fetchall()}
    for col, decl in (("book_odds", "INTEGER"), ("book_implied", "REAL")):
        if col not in hr_cols:
            conn.execute(f"ALTER TABLE hr_prop_candidates ADD COLUMN {col} {decl} DEFAULT NULL")
    conn.commit()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_parlays(parlays: list[dict], db_path: str = DB_PATH) -> list[int]:
    """
    Saves the day's generated parlays (2-leg pairs) and their legs to the database.
    Returns the parlay IDs. Skips if any parlay was already saved today.
    """
    if not parlays:
        return []
    conn  = _connect(db_path)
    today = str(date.today())

    existing = conn.execute(
        "SELECT id FROM parlays WHERE date=? LIMIT 1", (today,)
    ).fetchone()
    if existing:
        print(f"[backtest] Parlay(s) already logged for {today} — skipping.")
        conn.close()
        return []

    now = datetime.now(timezone.utc).isoformat()
    ids = [_insert_parlay(conn, p, now) for p in parlays]
    conn.commit()
    conn.close()
    return ids


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
    parlay_id = _insert_parlay(conn, parlay, now)
    conn.commit()
    conn.close()
    return parlay_id


def _insert_parlay(conn: sqlite3.Connection, parlay: dict, now: str) -> int:
    today = str(date.today())
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

    print(f"[backtest] Logged parlay #{parlay_id} ({parlay['num_legs']} legs, {_fmt_odds(parlay['combined_odds'])})")
    return parlay_id


def log_hr_candidates(candidates: list[dict], db_path: str = DB_PATH) -> list[int]:
    """Persists HR prop gate candidates so result_tracker can grade them next morning.
    Skips inserting if already logged today. Always returns today's candidate IDs
    in order, so log_hr_parlays can pair them."""
    if not candidates:
        return []
    conn  = _connect(db_path)
    today = str(date.today())

    existing_rows = conn.execute(
        "SELECT id, batter_id, book_odds FROM hr_prop_candidates WHERE date=?", (today,)
    ).fetchall()
    if existing_rows:
        # Backfill odds we have now but didn't before (recover from a failed odds fetch).
        by_bid = {str(c.get("batter_id")): c for c in candidates}
        backfilled = 0
        for r in existing_rows:
            c = by_bid.get(str(r["batter_id"]))
            if c and c.get("book_odds") is not None and r["book_odds"] is None:
                conn.execute("UPDATE hr_prop_candidates SET book_odds=?, book_implied=? WHERE id=?",
                             (c["book_odds"], c.get("book_implied"), r["id"]))
                backfilled += 1
        if backfilled:
            conn.commit()
            print(f"[backtest] Backfilled odds on {backfilled} previously-unpriced HR candidate(s).")
        else:
            print(f"[backtest] HR candidates already logged for {today} — skipping.")
    else:
        now = datetime.now(timezone.utc).isoformat()
        for c in candidates:
            s = c.get("scores", {})
            conn.execute(
                """INSERT INTO hr_prop_candidates
                   (date, game_pk, batter_id, batter_name, team, pitcher_id,
                    barrel_rate, sweet_spot, hard_contact, zone_fit, pitcher_hr_fb,
                    gate_triggered, book_odds, book_implied, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    c.get("book_odds"),
                    c.get("book_implied"),
                    now,
                ),
            )
        conn.commit()
        print(f"[backtest] Logged {len(candidates)} HR prop candidate(s) for {today}")

    rows = conn.execute(
        "SELECT id FROM hr_prop_candidates WHERE date=? ORDER BY id", (today,)
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def log_hr_parlays(cand_ids: list[int], stake: float = 10.0,
                   db_path: str = DB_PATH) -> None:
    """Logs 2-leg HR parlays (both batters to hit 1+ HR) as interleaved pairs,
    flat `stake` each, auto-priced from the two legs' 1+ HR odds. Needs at least
    two candidate IDs already in hr_prop_candidates (call log_hr_candidates first)."""
    if not cand_ids or len(cand_ids) < 2:
        return
    conn  = _connect(db_path)
    today = str(date.today())
    from parlay.parlay_builder import combine_odds

    existing = conn.execute(
        "SELECT id, leg1_id, leg2_id, odds FROM hr_parlays WHERE date=? ORDER BY parlay_num", (today,)
    ).fetchall()
    if existing:
        # Already logged today — re-price any parlay still missing odds (recover once the
        # candidate legs have been backfilled). Priced ones untouched.
        repriced = 0
        for row in existing:
            if row["odds"] is not None:
                continue
            pair = conn.execute(
                "SELECT book_odds FROM hr_prop_candidates WHERE id IN (?, ?)", (row["leg1_id"], row["leg2_id"])
            ).fetchall()
            combined = combine_odds([r["book_odds"] for r in pair])
            if combined is not None:
                conn.execute("UPDATE hr_parlays SET odds=? WHERE id=?", (combined, row["id"]))
                repriced += 1
        conn.commit()
        conn.close()
        if repriced:
            print(f"[backtest] Re-priced {repriced} previously-unpriced HR parlay(s) for {today}.")
        else:
            print(f"[backtest] HR parlays already logged for {today} — skipping.")
        return

    half = len(cand_ids) // 2
    now  = datetime.now(timezone.utc).isoformat()
    priced = 0
    for i in range(half):
        leg1_id, leg2_id = cand_ids[i], cand_ids[i + half]
        pair_odds = conn.execute(
            "SELECT book_odds FROM hr_prop_candidates WHERE id IN (?, ?)", (leg1_id, leg2_id)
        ).fetchall()
        combined = combine_odds([r["book_odds"] for r in pair_odds])
        if combined is not None:
            priced += 1
        conn.execute(
            """INSERT INTO hr_parlays (date, parlay_num, leg1_id, leg2_id, stake, odds, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (today, i + 1, leg1_id, leg2_id, stake, combined, now),
        )
    conn.commit()
    conn.close()
    print(f"[backtest] Logged {half} HR parlays for {today} (${stake:.0f} each) "
          f"— {priced}/{half} auto-priced from book odds")


def log_hit_parlay(legs: list[dict], db_path: str = DB_PATH) -> list[int]:
    """Persists hit parlay legs so result_tracker can grade them later.
    Re-runs on the same day are allowed if the batter set changed (new lineups confirmed).
    If the batter IDs are identical to what's already logged, skips silently.
    Always returns today's leg IDs in order."""
    if not legs:
        return []
    conn  = _connect(db_path)
    today = str(date.today())

    new_ids  = sorted(str(leg.get("batter_id", "")) for leg in legs)
    existing_rows = conn.execute(
        "SELECT id, batter_id FROM hit_legs WHERE date=? ORDER BY id", (today,)
    ).fetchall()
    existing_ids = sorted(str(r["batter_id"]) for r in existing_rows)

    if existing_ids == new_ids:
        # Same batters already logged. Backfill odds we have now but didn't before
        # (e.g. the prop-odds API failed on the first run, succeeded on this re-run).
        by_bid = {str(leg.get("batter_id")): leg for leg in legs}
        backfilled = 0
        for r in existing_rows:
            leg = by_bid.get(str(r["batter_id"]))
            if not leg or leg.get("book_odds") is None:
                continue
            cur = conn.execute("SELECT book_odds FROM hit_legs WHERE id=?", (r["id"],)).fetchone()
            if cur["book_odds"] is None:
                conn.execute(
                    "UPDATE hit_legs SET book_odds=?, book_implied=?, ev=? WHERE id=?",
                    (leg["book_odds"], leg.get("book_implied"), leg.get("ev"), r["id"]),
                )
                backfilled += 1
        if backfilled:
            conn.commit()
            print(f"[backtest] Backfilled odds on {backfilled} previously-unpriced hit leg(s).")
        else:
            print(f"[backtest] Hit legs already logged for {today} (same batters) — skipping.")
    else:
        if existing_rows:
            # Lineups updated since first run — replace stale legs. Any hit_parlays rows
            # built on those legs must go too: they reference leg IDs that are about to be
            # deleted, and an orphaned parlay can never roll up (its legs never resolve).
            old_ids = tuple(r["id"] for r in existing_rows)
            placeholders = ",".join("?" * len(old_ids))
            orphaned = conn.execute(
                f"DELETE FROM hit_parlays WHERE date=? AND (leg1_id IN ({placeholders}) "
                f"OR leg2_id IN ({placeholders}))",
                (today, *old_ids, *old_ids),
            ).rowcount
            conn.execute(f"DELETE FROM hit_legs WHERE id IN ({placeholders})", old_ids)
            print(f"[backtest] Lineup update detected — replacing {len(existing_rows)} stale hit leg(s)"
                  + (f" and {orphaned} dependent parlay row(s)." if orphaned else "."))
        now = datetime.now(timezone.utc).isoformat()
        for leg in legs:
            conn.execute(
                """INSERT INTO hit_legs
                   (date, game_pk, batter_id, batter_name, team, opponent_team,
                    pitcher_name, lineup_pos, hit_probability,
                    book_odds, book_implied, ev, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    leg.get("book_odds"),
                    leg.get("book_implied"),
                    leg.get("ev"),
                    now,
                ),
            )
        conn.commit()
        print(f"[backtest] Logged {len(legs)} hit leg(s) for {today} (individual batter rows)")

    rows = conn.execute(
        "SELECT id FROM hit_legs WHERE date=? ORDER BY id", (today,)
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def log_hit_parlays(leg_ids: list[int], base_stake: float = 25.0,
                    profit_floor: float = 25.0, max_stake: float = 50.0,
                    db_path: str = DB_PATH) -> None:
    """Logs the 2-leg parlay structure (interleaved pairs) for a --hits-2 run.
    Requires leg_ids to already be in the DB (call log_hit_parlay first).
    Each parlay is auto-priced from its two legs' 1+ hit odds and staked by the
    'double-up, else $25 floor' rule; unpriced parlays default to the base stake."""
    if not leg_ids:
        return
    conn  = _connect(db_path)
    today = str(date.today())
    from parlay.parlay_builder import combine_odds, recommended_stake

    existing = conn.execute(
        "SELECT id, leg1_id, leg2_id, odds FROM hit_parlays WHERE date=? ORDER BY parlay_num", (today,)
    ).fetchall()
    if existing:
        # Already logged today — re-price any parlay still missing odds (recover from a
        # prior failed odds fetch once the legs have been backfilled). Priced ones untouched.
        repriced = 0
        for row in existing:
            if row["odds"] is not None:
                continue
            pair = conn.execute(
                "SELECT book_odds FROM hit_legs WHERE id IN (?, ?)", (row["leg1_id"], row["leg2_id"])
            ).fetchall()
            combined = combine_odds([r["book_odds"] for r in pair])
            if combined is not None:
                stake = recommended_stake(combined, base_stake, profit_floor, max_stake)
                conn.execute("UPDATE hit_parlays SET odds=?, stake=? WHERE id=?",
                             (combined, stake, row["id"]))
                repriced += 1
        conn.commit()
        conn.close()
        if repriced:
            print(f"[backtest] Re-priced {repriced} previously-unpriced hit parlay(s) for {today}.")
        else:
            print(f"[backtest] Hit parlays already logged for {today} — skipping.")
        return

    half = len(leg_ids) // 2
    now  = datetime.now(timezone.utc).isoformat()
    priced = 0
    for i in range(half):
        leg1_id, leg2_id = leg_ids[i], leg_ids[i + half]
        # Auto-price the parlay from the two legs' fetched 1+ hit odds.
        pair_odds = conn.execute(
            "SELECT book_odds FROM hit_legs WHERE id IN (?, ?)", (leg1_id, leg2_id)
        ).fetchall()
        combined = combine_odds([r["book_odds"] for r in pair_odds])
        if combined is not None:
            priced += 1
            stake = recommended_stake(combined, base_stake, profit_floor, max_stake)
        else:
            stake = base_stake  # no book price yet — fall back to the base unit
        conn.execute(
            """INSERT INTO hit_parlays (date, parlay_num, leg1_id, leg2_id, stake, odds, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (today, i + 1, leg1_id, leg2_id, stake, combined, now),
        )
    conn.commit()
    conn.close()
    print(f"[backtest] Logged {half} hit parlays for {today} "
          f"— {priced}/{half} auto-priced & staked from book odds")


def record_hit_payout(date_str: str, parlay_num: int, payout_dollars: float,
                      db_path: str = DB_PATH) -> None:
    """Records the PROFIT (winnings) for a winning hit parlay — the slip's
    "to win" amount, not the total return. Total received = stake + this value."""
    conn = _connect(db_path)
    conn.execute(
        "UPDATE hit_parlays SET payout=? WHERE date=? AND parlay_num=?",
        (payout_dollars, date_str, parlay_num),
    )
    conn.commit()
    conn.close()
    print(f"[backtest] Recorded ${payout_dollars:.2f} payout for hit parlay #{parlay_num} on {date_str}")


def record_hit_odds(date_str: str, parlay_num: int, odds: int,
                    db_path: str = DB_PATH) -> None:
    """Records the combined American odds from the bet slip at placement time.
    If the parlay already resolved as a win with no payout entered, the payout
    is computed immediately; otherwise the roll-up computes it on grading."""
    conn = _connect(db_path)
    row  = conn.execute(
        "SELECT id, stake, outcome, payout FROM hit_parlays WHERE date=? AND parlay_num=?",
        (date_str, parlay_num),
    ).fetchone()
    if not row:
        conn.close()
        print(f"[backtest] No hit parlay #{parlay_num} found for {date_str}.")
        return

    conn.execute("UPDATE hit_parlays SET odds=? WHERE id=?", (odds, row["id"]))

    odds_str = f"+{odds}" if odds > 0 else str(odds)
    print(f"[backtest] Recorded {odds_str} odds for hit parlay #{parlay_num} on {date_str}")

    if row["outcome"] == "win" and row["payout"] is None:
        # payout column = PROFIT (winnings), so subtract the stake back out
        profit = round(row["stake"] * (_american_to_decimal(odds) - 1.0), 2)
        conn.execute("UPDATE hit_parlays SET payout=? WHERE id=?", (profit, row["id"]))
        print(f"[backtest] Parlay already won — profit auto-computed: ${profit:.2f}")

    conn.commit()
    conn.close()


def _american_to_decimal(odds: int) -> float:
    if odds > 0:
        return odds / 100.0 + 1.0
    return 100.0 / abs(odds) + 1.0


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
    hr_parlays   = conn.execute("SELECT * FROM hr_parlays WHERE outcome IS NOT NULL").fetchall()
    conn.close()

    # ── Game parlays ──────────────────────────────────────────────────────────
    parlay_wins   = sum(1 for p in parlays if p["outcome"] == "win")
    parlay_losses = sum(1 for p in parlays if p["outcome"] == "loss")
    parlay_total  = len(parlays)
    hit_rate      = parlay_wins / parlay_total if parlay_total else 0

    # Pushed parlays (every leg voided) refund the stake — excluded from staked/returned
    # so they land at net 0 instead of counting as a loss.
    gp_counted   = [p for p in parlays if p["outcome"] in ("win", "loss")]
    gp_staked    = len(gp_counted) * 10.0
    gp_returned  = sum((p["payout"] or 0) * 10.0 for p in gp_counted if p["outcome"] == "win")
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
    # The payout column holds PROFIT (winnings) — total received = stake + payout.
    # Net is therefore +profit on each win and −stake on each loss (voids are 0).
    hp_profit    = sum(p["payout"] for p in hp_resolved
                       if p["outcome"] == "win" and p["payout"] is not None)
    hp_loss_lost = sum(p["stake"] for p in hp_resolved if p["outcome"] == "loss")
    hp_wins_no_payout = sum(1 for p in hp_resolved
                            if p["outcome"] == "win" and p["payout"] is None)
    hp_net       = (hp_profit - hp_loss_lost) if hp_resolved and not hp_wins_no_payout else None

    # ── HR prop candidates ────────────────────────────────────────────────────
    # Voids (game never played) are excluded — they aren't a miss.
    hr_graded = [r for r in hr_cands if r["outcome"] in ("hr", "no_hr")]
    hr_hits   = sum(1 for r in hr_graded if r["outcome"] == "hr")
    hr_rate   = round(hr_hits / len(hr_graded), 4) if hr_graded else None

    # ── HR parlays (2-leg, both to hit 1+ HR) ─────────────────────────────────
    hrp_resolved  = [p for p in hr_parlays if p["outcome"] in ("win", "loss")]
    hrp_wins      = sum(1 for p in hrp_resolved if p["outcome"] == "win")
    hrp_losses    = sum(1 for p in hrp_resolved if p["outcome"] == "loss")
    hrp_staked    = sum(p["stake"] for p in hrp_resolved)
    hrp_profit    = sum(p["payout"] for p in hrp_resolved
                        if p["outcome"] == "win" and p["payout"] is not None)
    hrp_loss_lost = sum(p["stake"] for p in hrp_resolved if p["outcome"] == "loss")
    hrp_wins_no_payout = sum(1 for p in hrp_resolved
                             if p["outcome"] == "win" and p["payout"] is None)
    hrp_net       = (hrp_profit - hrp_loss_lost) if hrp_resolved and not hrp_wins_no_payout else None

    # ── Combined net ──────────────────────────────────────────────────────────
    # Empty categories contribute 0; "incomplete" means a WIN is still awaiting a
    # payout entry (not merely that a bet type has no resolved parlays yet).
    combined_net = gp_net + (hp_net if hp_net is not None else 0) + (hrp_net if hrp_net is not None else 0)
    combined_complete = (hp_wins_no_payout == 0) and (hrp_wins_no_payout == 0)

    summary = {
        # game parlays
        "parlays_tracked":      parlay_total,
        "parlay_hit_rate":      round(hit_rate, 4),
        "gp_wins":              parlay_wins,
        "gp_losses":            parlay_losses,
        "gp_staked":            round(gp_staked, 2),
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
        "hp_profit":            round(hp_profit, 2),
        "hp_net":               round(hp_net, 2) if hp_net is not None else None,
        "hp_roi":               round(hp_net / hp_staked, 4) if (hp_net is not None and hp_staked) else None,
        "hp_wins_no_payout":    hp_wins_no_payout,
        # hr props
        "hr_cands_graded":      len(hr_graded),
        "hr_hit_rate":          hr_rate,
        # hr parlays
        "hrp_tracked":          len(hrp_resolved),
        "hrp_wins":             hrp_wins,
        "hrp_losses":           hrp_losses,
        "hrp_staked":           round(hrp_staked, 2),
        "hrp_profit":           round(hrp_profit, 2),
        "hrp_net":              round(hrp_net, 2) if hrp_net is not None else None,
        "hrp_roi":              round(hrp_net / hrp_staked, 4) if (hrp_net is not None and hrp_staked) else None,
        "hrp_wins_no_payout":   hrp_wins_no_payout,
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


def _print_pending_payouts(db_path: str = DB_PATH) -> None:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT hp.date, hp.parlay_num,
               hl1.batter_name AS leg1, hl2.batter_name AS leg2
        FROM hit_parlays hp
        JOIN hit_legs hl1 ON hl1.id = hp.leg1_id
        JOIN hit_legs hl2 ON hl2.id = hp.leg2_id
        WHERE hp.outcome = 'win' AND hp.payout IS NULL
        ORDER BY hp.date, hp.parlay_num
    """).fetchall()
    conn.close()
    for r in rows:
        print(f"    {r['date']}  #{r['parlay_num']}  "
              f"({r['leg1']} + {r['leg2']})")
        print(f"    → python main.py --record-hit-win {r['date']} {r['parlay_num']} <AMOUNT>")
    if rows:
        print("    (tip: record odds at bet time with --record-hit-placed and this step disappears)")


def _print_summary(s: dict) -> None:
    w = 40
    print(f"\n{'=' * w}")

    # ── Combined P&L ─────────────────────────────────────────────────────────
    net = s["combined_net"]
    net_str = f"${net:+.2f}" if s["combined_complete"] else f"${net:+.2f} (payouts pending)"
    print(f"  NET P&L (all bets):  {net_str}")
    print(f"{'─' * w}")

    # ── Game parlays ──────────────────────────────────────────────────────────
    print(f"  GAME PARLAYS  ({s['parlays_tracked']} tracked)")
    print(f"{'─' * w}")
    if s["parlays_tracked"]:
        print(f"  Won/Lost:   {s['gp_wins']}/{s['gp_losses']}  "
              f"({s['gp_wins'] / max(s['gp_wins'] + s['gp_losses'], 1) * 100:.0f}%)")
        print(f"  Staked:     ${s['gp_staked']:.2f}")
        print(f"  Net P&L:    ${s['gp_net']:+.2f}  ({s['roi']*100:+.1f}% ROI)")
    if s["avg_clv"] is not None:
        print(f"  Avg CLV:    {s['avg_clv'] * 100:+.2f}%")
    if s["ml_win_rate"] is not None:
        print(f"  ML legs:    {s['ml_win_rate'] * 100:.1f}%  ({s['ml_legs_graded']} graded)")
    if s["total_win_rate"] is not None:
        print(f"  Totals:     {s['total_win_rate'] * 100:.1f}%  ({s['total_legs_graded']} graded)")

    # ── Hit parlays ───────────────────────────────────────────────────────────
    print(f"{'─' * w}")
    print(f"  HIT PARLAYS  ({s['hp_tracked']} tracked)")
    print(f"{'─' * w}")
    if s["hp_tracked"]:
        print(f"  Won/Lost:   {s['hp_wins']}/{s['hp_losses']}  "
              f"({s['hp_wins'] / max(s['hp_wins'] + s['hp_losses'], 1) * 100:.0f}%)")
        print(f"  Staked:     ${s['hp_staked']:.2f}")
        if s["hp_net"] is not None:
            roi_str = f"  ({s['hp_roi']*100:+.1f}% ROI)" if s["hp_roi"] is not None else ""
            print(f"  Net P&L:    ${s['hp_net']:+.2f}{roi_str}")
        else:
            profit_str = f"${s['hp_profit']:.2f} confirmed" if s["hp_profit"] else "—"
            print(f"  Profit:     {profit_str}")
            if s["hp_wins_no_payout"]:
                print(f"  Note: {s['hp_wins_no_payout']} win(s) need payout entered:")
                _print_pending_payouts()
    if s["hit_leg_win_rate"] is not None:
        print(f"  Leg rate:   {s['hit_leg_win_rate'] * 100:.1f}%  ({s['hit_legs_graded']} legs)")

    # ── HR parlays (2-leg, both to homer) ─────────────────────────────────────
    print(f"{'─' * w}")
    print(f"  HR PARLAYS  ({s['hrp_tracked']} tracked)")
    print(f"{'─' * w}")
    if s["hrp_tracked"]:
        print(f"  Won/Lost:   {s['hrp_wins']}/{s['hrp_losses']}  "
              f"({s['hrp_wins'] / max(s['hrp_wins'] + s['hrp_losses'], 1) * 100:.0f}%)")
        print(f"  Staked:     ${s['hrp_staked']:.2f}")
        if s["hrp_net"] is not None:
            roi_str = f"  ({s['hrp_roi']*100:+.1f}% ROI)" if s["hrp_roi"] is not None else ""
            print(f"  Net P&L:    ${s['hrp_net']:+.2f}{roi_str}")
        elif s["hrp_wins_no_payout"]:
            print(f"  Net P&L:    pending ({s['hrp_wins_no_payout']} win payout(s) unpriced)")
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
