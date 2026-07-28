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
    _roll_up_hr_parlays(db_path)


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
        games = _fetch_day_games(date_str)
        for leg in legs:
            key = (leg["home_team"], leg["away_team"])
            game = games.get(key)
            if not game or game["state"] == "pending":
                print(f"[result_tracker] Not final yet: {leg['away_team']} @ {leg['home_team']} ({date_str})")
                continue

            if game["state"] == "no_contest":
                # Game never played — the leg is removed from the parlay (push/refund),
                # not a loss. The roll-up then re-prices from the surviving legs.
                conn = _connect(db_path)
                conn.execute("UPDATE legs SET outcome='push' WHERE id=?", (leg["id"],))
                conn.commit()
                conn.close()
                print(f"[result_tracker] Leg {leg['id']} ({leg['display']}) → push (game not played)")
                resolved += 1
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
        state = _game_result_state(row["game_pk"])
        if state == "pending":
            print(f"[result_tracker] {row['batter_name']}: game {row['game_pk']} not final yet — skipping.")
            continue
        if state == "no_contest":
            conn = _connect(db_path)
            conn.execute("UPDATE hit_legs SET outcome='void' WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
            print(f"[result_tracker] Hit leg — {row['batter_name']}: game not played → void")
            resolved += 1
            continue

        hits = _fetch_batter_hits(row["game_pk"], row["batter_id"])
        if hits is None:
            print(f"[result_tracker] {row['batter_name']}: not in box score for {row['game_pk']} — skipping.")
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
        state = _game_result_state(row["game_pk"])
        if state == "pending":
            print(f"[result_tracker] {row['batter_name']}: game {row['game_pk']} not final yet — skipping.")
            continue
        if state == "no_contest":
            conn = _connect(db_path)
            conn.execute("UPDATE hr_prop_candidates SET outcome='void' WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
            print(f"[result_tracker] HR prop — {row['batter_name']}: game not played → void")
            resolved += 1
            continue

        hrs = _fetch_batter_batting_stat(row["game_pk"], row["batter_id"], "homeRuns")
        if hrs is None:
            print(f"[result_tracker] {row['batter_name']}: not in box score for {row['game_pk']} — skipping.")
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
            "SELECT outcome, odds FROM legs WHERE parlay_id=?", (pid,)
        ).fetchall()
        outcomes = [l["outcome"] for l in legs]

        if None in outcomes:
            continue  # still waiting on at least one leg

        # Pushed legs (cancelled/postponed game, or a total landing exactly on the line)
        # are dropped from the parlay. The payout must be re-priced from the legs that
        # actually stood — using the original combined odds would overstate the win.
        surviving = [l for l in legs if l["outcome"] != "push"]

        if not surviving:
            result, payout = "push", 1.0        # every leg voided — stake refunded
        elif any(l["outcome"] == "loss" for l in surviving):
            result, payout = "loss", 0.0
        else:
            result = "win"
            decimal = 1.0
            for leg in surviving:
                if leg["odds"] is None:
                    decimal = None
                    break
                decimal *= _american_to_decimal(leg["odds"])
            # Fall back to the stored combined odds only if a leg's price is missing
            payout = decimal if decimal is not None else _american_to_decimal(row["combined_odds"])

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
        l1 = conn.execute("SELECT outcome, book_odds FROM hit_legs WHERE id=?", (row["leg1_id"],)).fetchone()
        l2 = conn.execute("SELECT outcome, book_odds FROM hit_legs WHERE id=?", (row["leg2_id"],)).fetchone()
        conn.close()

        o1 = l1["outcome"] if l1 else None
        o2 = l2["outcome"] if l2 else None

        if o1 is None or o2 is None:
            continue  # leg(s) not graded yet

        # payout column = PROFIT (winnings), not total return: void refunds → 0 profit,
        # loss → 0, win → stake × (decimal_odds − 1) when bet-slip odds were recorded.
        if o1 == "void" and o2 == "void":
            result, payout = "void", 0.0
        elif "loss" in (o1, o2):
            result, payout = "loss", 0.0
        elif o1 == "win" and o2 == "win":
            result = "win"
            odds   = row["odds"] if "odds" in row.keys() else None
            payout = round(row["stake"] * (_american_to_decimal(odds) - 1.0), 2) if odds else None
        else:
            # One void + one win: the book drops the voided leg and the parlay pays out
            # as a straight bet on the survivor, at that leg's own (shorter) odds.
            result = "win"
            survivor = l1 if o1 == "win" else l2
            s_odds   = survivor["book_odds"] if survivor else None
            payout   = (round(row["stake"] * (_american_to_decimal(s_odds) - 1.0), 2)
                        if s_odds else None)

        conn = _connect(db_path)
        conn.execute("UPDATE hit_parlays SET outcome=?, payout=? WHERE id=?",
                     (result, payout, row["id"]))
        conn.commit()
        conn.close()

        payout_str = f"${payout:.2f}" if payout is not None else "unknown — enter with --record-hit-win"
        print(f"[result_tracker] Hit parlay #{row['parlay_num']} ({row['date']}) "
              f"→ {result}  (payout: {payout_str})")
        resolved += 1

    if resolved:
        print(f"[result_tracker] {resolved} hit parlay(s) graded.")


def _roll_up_hr_parlays(db_path: str) -> None:
    """Grades 2-leg HR parlays once both legs are graded: win only if BOTH batters
    homered. Payout is profit from the stored combined odds (profit convention)."""
    conn    = _connect(db_path)
    pending = conn.execute("SELECT * FROM hr_parlays WHERE outcome IS NULL").fetchall()
    conn.close()

    if not pending:
        return

    resolved = 0
    for row in pending:
        conn = _connect(db_path)
        l1 = conn.execute("SELECT outcome, book_odds FROM hr_prop_candidates WHERE id=?", (row["leg1_id"],)).fetchone()
        l2 = conn.execute("SELECT outcome, book_odds FROM hr_prop_candidates WHERE id=?", (row["leg2_id"],)).fetchone()
        conn.close()

        o1 = l1["outcome"] if l1 else None
        o2 = l2["outcome"] if l2 else None
        if o1 is None or o2 is None:
            continue  # leg(s) not graded yet

        # payout = PROFIT (winnings). Void legs (game never played) refund rather than lose.
        if o1 == "void" and o2 == "void":
            result, payout = "void", 0.0
        elif "no_hr" in (o1, o2):
            result, payout = "loss", 0.0
        elif o1 == "hr" and o2 == "hr":
            result = "win"
            odds   = row["odds"] if "odds" in row.keys() else None
            payout = round(row["stake"] * (_american_to_decimal(odds) - 1.0), 2) if odds else None
        else:
            # One void + one HR: pays out as a straight bet on the survivor's own odds.
            result = "win"
            survivor = l1 if o1 == "hr" else l2
            s_odds   = survivor["book_odds"] if survivor else None
            payout   = (round(row["stake"] * (_american_to_decimal(s_odds) - 1.0), 2)
                        if s_odds else None)

        conn = _connect(db_path)
        conn.execute("UPDATE hr_parlays SET outcome=?, payout=? WHERE id=?",
                     (result, payout, row["id"]))
        conn.commit()
        conn.close()

        payout_str = f"${payout:.2f}" if payout is not None else "unknown (no stored odds)"
        print(f"[result_tracker] HR parlay #{row['parlay_num']} ({row['date']}) "
              f"→ {result}  (payout: {payout_str})")
        resolved += 1

    if resolved:
        print(f"[result_tracker] {resolved} HR parlay(s) graded.")


# ── MLB Stats API helpers ─────────────────────────────────────────────────────

def _fetch_day_games(date_str: str) -> dict[tuple, dict]:
    """Returns {(home_team, away_team): {state, home_score, away_score}} for a date.

    state is 'final' | 'no_contest' | 'pending'. Postponed/cancelled games report
    abstractGameState "Final" with no real score, so they're classified separately —
    grading them as Final would score a phantom 0-0.
    """
    url = f"{MLB_API}/schedule"
    resp = requests.get(url, params={"sportId": 1, "date": date_str, "hydrate": "linescore"},
                        timeout=10)
    resp.raise_for_status()

    games: dict[tuple, dict] = {}
    for date_entry in resp.json().get("dates", []):
        for game in date_entry.get("games", []):
            status   = game.get("status", {})
            detailed = status.get("detailedState", "").lower()
            if any(k in detailed for k in _NO_CONTEST_STATES):
                state = "no_contest"
            elif "suspended" in detailed:
                state = "pending"          # resumes later
            elif status.get("abstractGameState") == "Final":
                state = "final"
            else:
                state = "pending"

            home = game["teams"]["home"]["team"]["name"]
            away = game["teams"]["away"]["team"]["name"]
            games[(home, away)] = {
                "state":      state,
                "home_score": game["teams"]["home"].get("score", 0),
                "away_score": game["teams"]["away"].get("score", 0),
            }

    return games


# A game whose detailedState contains any of these never produced an official result.
# ("Suspended" games resume later, so they stay pending rather than voiding.)
_NO_CONTEST_STATES = ("postponed", "cancelled", "canceled")

_GAME_STATE_CACHE: dict[int, tuple[str, str]] = {}


def _fetch_game_state(game_pk: int) -> tuple[str, str] | None:
    """Returns (abstractGameState, detailedState) for one game, cached per run."""
    if game_pk in _GAME_STATE_CACHE:
        return _GAME_STATE_CACHE[game_pk]
    try:
        resp = requests.get(f"{MLB_API}/schedule",
                            params={"sportId": 1, "gamePks": game_pk}, timeout=10)
        resp.raise_for_status()
        for date_entry in resp.json().get("dates", []):
            for game in date_entry.get("games", []):
                if game.get("gamePk") == game_pk:
                    status = game.get("status", {})
                    state = (status.get("abstractGameState", ""),
                             status.get("detailedState", ""))
                    _GAME_STATE_CACHE[game_pk] = state
                    return state
    except Exception as e:
        print(f"[result_tracker] Game status fetch failed ({game_pk}): {e}")
    return None


def _game_result_state(game_pk: int) -> str:
    """Classifies a game for grading: 'final' | 'no_contest' | 'pending'.

    Box scores populate live (and pre-game), so player stats MUST NOT be graded
    until this returns 'final' — otherwise a batter sitting on 0 hits in the 3rd
    inning gets permanently recorded as a loss.
    """
    state = _fetch_game_state(game_pk)
    if state is None:
        return "pending"
    abstract, detailed = state
    low = detailed.lower()
    if any(k in low for k in _NO_CONTEST_STATES):
        return "no_contest"      # never played — void, not a loss
    if "suspended" in low:
        return "pending"         # resumes later
    return "final" if abstract == "Final" else "pending"


def _fetch_batter_batting_stat(game_pk: int, batter_id: int, stat_key: str) -> int | None:
    """Returns a batting stat (e.g. 'hits', 'homeRuns') for a batter from the box score.
    Callers must check _game_result_state() first — this endpoint returns live/partial
    stats for games in progress and zeros for games that haven't started."""
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
