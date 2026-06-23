import requests
from output.backtest import _connect, DB_PATH

MLB_API = "https://statsapi.mlb.com/api/v1"


def resolve_pending(db_path: str = DB_PATH) -> None:
    """
    Grades all unresolved parlay legs, hit legs, and HR prop candidates, then rolls up parlay outcomes.
    Run after games finish (e.g. next morning with --results).
    """
    _resolve_parlay_legs(db_path)
    _resolve_hit_legs(db_path)
    _resolve_hr_prop_legs(db_path)
    _roll_up_parlays(db_path)
    _roll_up_hit_parlays(db_path)


# ── Moneyline / total legs ────────────────────────────────────────────────────

def _resolve_parlay_legs(db_path: str) -> None:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT l.id, l.home_team, l.away_team, l.bet_type, l.side, l.line, l.display,
               p.date
        FROM legs l
        JOIN parlays p ON l.parlay_id = p.id
        WHERE l.outcome IS NULL
    """).fetchall()
    conn.close()

    if not rows:
        print("[result_tracker] No pending parlay legs.")
        return

    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(dict(row))

    resolved = 0
    for date_str, legs in by_date.items():
        scores = _fetch_scores(date_str)
        for leg in legs:
            key = (leg["home_team"], leg["away_team"])
            game = scores.get(key)
            if not game:
                print(f"[result_tracker] No final score: {leg['away_team']} @ {leg['home_team']} ({date_str})")
                continue

            outcome = _grade_ml_total(leg, game)
            if outcome:
                conn = _connect(db_path)
                conn.execute("UPDATE legs SET outcome=? WHERE id=?", (outcome, leg["id"]))
                conn.commit()
                conn.close()
                print(f"[result_tracker] Leg {leg['id']} ({leg['display']}) → {outcome}")
                resolved += 1

    print(f"[result_tracker] {resolved} parlay leg(s) graded.")


def _grade_ml_total(leg: dict, game: dict) -> str | None:
    home = game["home_score"]
    away = game["away_score"]

    if leg["bet_type"] == "ml":
        home_won = home > away
        if leg["side"] == "home":
            return "win" if home_won else "loss"
        else:
            return "win" if not home_won else "loss"

    if leg["bet_type"] == "total":
        total = home + away
        line  = leg["line"]
        if leg["side"] == "over":
            return "win" if total > line else ("push" if total == line else "loss")
        else:
            return "win" if total < line else ("push" if total == line else "loss")

    return None


# ── Hit legs ──────────────────────────────────────────────────────────────────

def _resolve_hit_legs(db_path: str) -> None:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT id, game_pk, batter_id, batter_name FROM hit_legs WHERE outcome IS NULL"
    ).fetchall()
    conn.close()

    if not rows:
        print("[result_tracker] No pending hit legs.")
        return

    resolved = 0
    for row in rows:
        hits = _fetch_batter_hits(row["game_pk"], row["batter_id"])
        if hits is None:
            print(f"[result_tracker] Game {row['game_pk']} not final yet (or batter not found).")
            continue

        outcome = "win" if hits >= 1 else "loss"
        conn = _connect(db_path)
        conn.execute("UPDATE hit_legs SET outcome=?, actual_hits=? WHERE id=?",
                     (outcome, hits, row["id"]))
        conn.commit()
        conn.close()
        print(f"[result_tracker] Hit leg — {row['batter_name']}: {hits} hit(s) → {outcome}")
        resolved += 1

    print(f"[result_tracker] {resolved} hit leg(s) graded.")


# ── HR prop candidates ────────────────────────────────────────────────────────

def _resolve_hr_prop_legs(db_path: str) -> None:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT id, game_pk, batter_id, batter_name FROM hr_prop_candidates WHERE outcome IS NULL"
    ).fetchall()
    conn.close()

    if not rows:
        print("[result_tracker] No pending HR prop candidates.")
        return

    resolved = 0
    for row in rows:
        hrs = _fetch_batter_batting_stat(row["game_pk"], row["batter_id"], "homeRuns")
        if hrs is None:
            print(f"[result_tracker] Game {row['game_pk']} not final yet (or batter not found).")
            continue

        outcome = "hr" if hrs >= 1 else "no_hr"
        conn = _connect(db_path)
        conn.execute("UPDATE hr_prop_candidates SET outcome=?, actual_hrs=? WHERE id=?",
                     (outcome, hrs, row["id"]))
        conn.commit()
        conn.close()
        print(f"[result_tracker] HR prop — {row['batter_name']}: {hrs} HR(s) → {outcome}")
        resolved += 1

    print(f"[result_tracker] {resolved} HR prop candidate(s) graded.")


# ── Parlay roll-up ────────────────────────────────────────────────────────────

def _roll_up_parlays(db_path: str) -> None:
    conn = _connect(db_path)
    pending = conn.execute(
        "SELECT id, combined_odds FROM parlays WHERE outcome IS NULL"
    ).fetchall()

    for row in pending:
        pid = row["id"]
        legs = conn.execute(
            "SELECT outcome FROM legs WHERE parlay_id=?", (pid,)
        ).fetchall()
        outcomes = [l["outcome"] for l in legs]

        if None in outcomes:
            continue  # still waiting on at least one leg

        active = [o for o in outcomes if o != "push"]
        if not active or all(o == "win" for o in active):
            result = "win"
            payout = _american_to_decimal(row["combined_odds"])
        elif any(o == "loss" for o in active):
            result = "loss"
            payout = 0.0
        else:
            result = "push"
            payout = 1.0

        conn.execute("UPDATE parlays SET outcome=?, payout=? WHERE id=?",
                     (result, payout, pid))
        print(f"[result_tracker] Parlay #{pid} → {result} (payout: {payout:.2f}x)")

    conn.commit()
    conn.close()


def _american_to_decimal(odds: int) -> float:
    if odds > 0:
        return odds / 100.0 + 1.0
    return 100.0 / abs(odds) + 1.0


# ── Hit parlay roll-up ────────────────────────────────────────────────────────

def _roll_up_hit_parlays(db_path: str) -> None:
    conn    = _connect(db_path)
    pending = conn.execute(
        "SELECT * FROM hit_parlays WHERE outcome IS NULL"
    ).fetchall()
    conn.close()

    if not pending:
        return

    resolved = 0
    for row in pending:
        conn = _connect(db_path)
        l1 = conn.execute("SELECT outcome FROM hit_legs WHERE id=?", (row["leg1_id"],)).fetchone()
        l2 = conn.execute("SELECT outcome FROM hit_legs WHERE id=?", (row["leg2_id"],)).fetchone()
        conn.close()

        o1 = l1["outcome"] if l1 else None
        o2 = l2["outcome"] if l2 else None

        if o1 is None or o2 is None:
            continue  # leg(s) not graded yet

        if o1 == "void" and o2 == "void":
            result, payout = "void", row["stake"]
        elif "loss" in (o1, o2):
            result, payout = "loss", 0.0
        else:
            # both win, or one void + one win (collapses to single-leg — payout TBD)
            result, payout = "win", None

        conn = _connect(db_path)
        conn.execute("UPDATE hit_parlays SET outcome=?, payout=? WHERE id=?",
                     (result, payout, row["id"]))
        conn.commit()
        conn.close()

        payout_str = f"${payout:.2f}" if payout is not None else "enter with --record-hit-win"
        print(f"[result_tracker] Hit parlay #{row['parlay_num']} ({row['date']}) "
              f"→ {result}  (payout: {payout_str})")
        resolved += 1

    if resolved:
        print(f"[result_tracker] {resolved} hit parlay(s) graded.")


# ── MLB Stats API helpers ─────────────────────────────────────────────────────

def _fetch_scores(date_str: str) -> dict[tuple, dict]:
    """Returns {(home_team, away_team): {home_score, away_score}} for all final games."""
    url = f"{MLB_API}/schedule"
    resp = requests.get(url, params={"sportId": 1, "date": date_str, "hydrate": "linescore"},
                        timeout=10)
    resp.raise_for_status()

    scores: dict[tuple, dict] = {}
    for date_entry in resp.json().get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            home = game["teams"]["home"]["team"]["name"]
            away = game["teams"]["away"]["team"]["name"]
            scores[(home, away)] = {
                "home_score": game["teams"]["home"].get("score", 0),
                "away_score": game["teams"]["away"].get("score", 0),
            }

    return scores


def _fetch_batter_batting_stat(game_pk: int, batter_id: int, stat_key: str) -> int | None:
    """Returns a batting stat (e.g. 'hits', 'homeRuns') for a batter from the box score,
    or None if the game isn't final or the batter doesn't appear."""
    url = f"{MLB_API}/game/{game_pk}/boxscore"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[result_tracker] Box score fetch failed ({game_pk}): {e}")
        return None

    for side in ("home", "away"):
        players = data.get("teams", {}).get(side, {}).get("players", {})
        player = players.get(f"ID{batter_id}")
        if player:
            val = player.get("stats", {}).get("batting", {}).get(stat_key)
            if val is None:
                return None
            return int(val)

    return None


def _fetch_batter_hits(game_pk: int, batter_id: int) -> int | None:
    return _fetch_batter_batting_stat(game_pk, batter_id, "hits")
