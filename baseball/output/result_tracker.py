import requests
from output.backtest import _connect, DB_PATH
from parlay.parlay_builder import _american_to_decimal

MLB_API = "https://statsapi.mlb.com/api/v1"


def resolve_pending(db_path: str = DB_PATH) -> None:
    """
    Grades all unresolved hit legs and HR prop candidates, then rolls up their parlays.
    Run after games finish (e.g. next morning with --results).
    Finishes with a summary spelling out exactly what hit and the net for the run.
    """
    _resolve_hit_legs(db_path)
    _resolve_hr_prop_legs(db_path)
    graded  = _roll_up_pairs(db_path, "hit")
    graded += _roll_up_pairs(db_path, "HR")
    _print_results_summary(graded)


def _print_results_summary(graded: list[dict]) -> None:
    """Spells out what hit this run — winners with their legs and payout, losers in one
    line each, then the net. Without this a win is just one flat log line among dozens."""
    w = 58
    if not graded:
        print(f"\n{'═' * w}\n  RESULTS — nothing newly graded this run\n{'═' * w}\n")
        return

    wins   = [g for g in graded if g["outcome"] == "win"]
    losses = [g for g in graded if g["outcome"] == "loss"]
    voids  = [g for g in graded if g["outcome"] in ("void", "push")]
    net    = sum(g["profit"] for g in graded if g["profit"] is not None)
    unknown = [g for g in graded if g["profit"] is None]

    print(f"\n{'═' * w}")
    print(f"  RESULTS — what hit")
    print(f"{'═' * w}")

    if wins:
        for g in wins:
            amt = f"+${g['profit']:.2f}" if g["profit"] is not None else "payout TBD"
            print(f"\n  ✓✓ {g['kind']} {g['label']}  ({g['date']})".ljust(w - len(amt)) + amt)
            for leg in g["legs"]:
                print(f"       • {leg}")
            odds_str = f"{g['odds']:+}" if g.get("odds") is not None else "—"
            print(f"       {odds_str}  |  ${g['stake']:.2f} staked"
                  + (f" → ${g['stake'] + g['profit']:.2f} back" if g["profit"] is not None else ""))
    else:
        print(f"\n  No winners this run.")

    if losses or voids:
        print()
        for g in losses:
            amt = f"-${g['stake']:.2f}"
            print(f"  ✗  {g['kind']} {g['label']}  ({g['date']})".ljust(w - len(amt)) + amt)
        for g in voids:
            print(f"  –  {g['kind']} {g['label']}  ({g['date']})".ljust(w - 8) + "voided")

    print(f"\n{'─' * w}")
    print(f"  Graded this run:  {len(wins)} won / {len(losses)} lost"
          + (f" / {len(voids)} void" if voids else ""))
    print(f"  Net:              ${net:+.2f}"
          + ("  (+ payouts still TBD)" if unknown else ""))
    print(f"{'═' * w}\n")


# ── Hit legs ──────────────────────────────────────────────────────────────────

def _resolve_hit_legs(db_path: str) -> None:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT id, date, game_pk, batter_id, batter_name FROM hit_legs WHERE outcome IS NULL"
    ).fetchall()
    conn.close()

    if not rows:
        print("[result_tracker] No pending hit legs.")
        return

    resolved = 0
    for row in rows:
        state = _game_result_state(row["game_pk"], row["date"])
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
        print(f"[result_tracker] Hit leg — {row['batter_name']}: {hits} hit(s) → {outcome.upper() + ' ✓' if outcome == 'win' else outcome}")
        resolved += 1

    print(f"[result_tracker] {resolved} hit leg(s) graded.")


# ── HR prop candidates ────────────────────────────────────────────────────────

def _resolve_hr_prop_legs(db_path: str) -> None:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT id, date, game_pk, batter_id, batter_name FROM hr_prop_candidates "
        "WHERE outcome IS NULL"
    ).fetchall()
    conn.close()

    if not rows:
        print("[result_tracker] No pending HR prop candidates.")
        return

    resolved = 0
    for row in rows:
        state = _game_result_state(row["game_pk"], row["date"])
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
        print(f"[result_tracker] HR prop — {row['batter_name']}: {hrs} HR(s) → {outcome.upper() + ' ✓' if outcome == 'hr' else outcome}")
        resolved += 1

    print(f"[result_tracker] {resolved} HR prop candidate(s) graded.")


# ── Two-leg parlay roll-up (hit and HR) ───────────────────────────────────────

# What differs between the two kinds of 2-leg parlay; the grading itself is shared.
# Table names are these literals, never input (see backtest._ensure_schema on why an
# identifier cannot be a bound parameter).
_PAIR_KINDS = {
    "hit": {"kind": "HIT PARLAY", "parlays": "hit_parlays", "legs": "hit_legs",
            "won": "win", "lost": "loss", "market": "Hit",
            "no_odds": "unknown — enter with --record-hit-win"},
    "HR":  {"kind": "HR PARLAY", "parlays": "hr_parlays", "legs": "hr_prop_candidates",
            "won": "hr", "lost": "no_hr", "market": "HR",
            "no_odds": "unknown (no stored odds)"},
}


def _roll_up_pairs(db_path: str, noun: str) -> list[dict]:
    """Grades the `noun` ('hit' | 'HR') 2-leg parlays whose legs are both graded.
    Returns a record per newly-graded parlay for the summary.

    payout = PROFIT (winnings), not total return: two voids refund (0 profit), a loss
    pays 0, and a win pays stake × (decimal − 1) at the stored combined odds. One void
    plus one win pays as a straight bet on the survivor, at that leg's own (shorter)
    odds, which is how the book settles it.
    """
    spec = _PAIR_KINDS[noun]
    won  = spec["won"]
    conn = _connect(db_path)
    pending = conn.execute(f"SELECT * FROM {spec['parlays']} WHERE outcome IS NULL").fetchall()

    graded: list[dict] = []
    for row in pending:
        l1, l2 = (conn.execute(f"SELECT outcome, book_odds, batter_name FROM {spec['legs']} "
                               f"WHERE id=?", (leg_id,)).fetchone()
                  for leg_id in (row["leg1_id"], row["leg2_id"]))
        o1 = l1["outcome"] if l1 else None
        o2 = l2["outcome"] if l2 else None
        if o1 is None or o2 is None:
            continue  # leg(s) not graded yet

        if o1 == "void" and o2 == "void":
            result, payout = "void", 0.0
        elif spec["lost"] in (o1, o2):
            result, payout = "loss", 0.0
        elif o1 == won and o2 == won:
            result = "win"
            payout = (round(row["stake"] * (_american_to_decimal(row["odds"]) - 1.0), 2)
                      if row["odds"] else None)
        else:
            result   = "win"
            survivor = l1 if o1 == won else l2
            s_odds   = survivor["book_odds"] if survivor else None
            payout   = (round(row["stake"] * (_american_to_decimal(s_odds) - 1.0), 2)
                        if s_odds else None)

        conn.execute(f"UPDATE {spec['parlays']} SET outcome=?, payout=? WHERE id=?",
                     (result, payout, row["id"]))
        conn.commit()

        payout_str = f"${payout:.2f}" if payout is not None else spec["no_odds"]
        print(f"[result_tracker] {spec['market']} parlay #{row['parlay_num']} ({row['date']}) "
              f"→ {result.upper() if result == 'win' else result}  (payout: {payout_str})")

        def _leg_label(leg, outcome):
            if not leg:
                return "?"
            odds = f" ({leg['book_odds']:+})" if leg["book_odds"] is not None else ""
            tag  = "" if outcome == won else f" [{outcome}]"
            return f"{leg['batter_name']} 1+ {spec['market']}{odds}{tag}"

        profit = payout if result == "win" else (-row["stake"] if result == "loss" else 0.0)
        graded.append({
            "kind":   spec["kind"], "label": f"#{row['parlay_num']}", "date": row["date"],
            "outcome": result, "stake": row["stake"], "profit": profit,
            "odds":   row["odds"],
            "legs":   [_leg_label(l1, o1), _leg_label(l2, o2)],
        })

    conn.close()
    if graded:
        print(f"[result_tracker] {len(graded)} {noun} parlay(s) graded.")
    return graded


# ── MLB Stats API helpers ─────────────────────────────────────────────────────

# A game whose detailedState contains any of these never produced an official result.
# ("Suspended" games resume later, so they stay pending rather than voiding.)
_NO_CONTEST_STATES = ("postponed", "cancelled", "canceled")

# Terminal results only ('final' | 'no_contest'), keyed (game_pk, slate). This module
# lives as long as the web process, so caching a pending state froze a suspended or
# not-yet-final game as pending until the next redeploy.
_GAME_STATE_CACHE: dict[tuple[int, str | None], str] = {}


def _fetch_game_entries(game_pk: int) -> list[dict] | None:
    """Every schedule entry for one game, earliest slate first; None if the fetch failed.

    A game is listed once per date it was scheduled on. A postponed game appears on its
    original date as 'Postponed' (abstractGameState 'Final', no result) and again on the
    makeup date. Both entries carry the MAKEUP date as officialDate, so the slate is read
    from the enclosing date block, not from the game.
    """
    try:
        resp = requests.get(f"{MLB_API}/schedule",
                            params={"sportId": 1, "gamePks": game_pk}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[result_tracker] Game status fetch failed ({game_pk}): {e}")
        return None
    entries = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("gamePk") == game_pk:
                status = game.get("status", {})
                entries.append({"slate":    date_entry.get("date") or "",
                                "abstract": status.get("abstractGameState", ""),
                                "detailed": status.get("detailedState", "")})
    return sorted(entries, key=lambda e: e["slate"])


def _game_result_state(game_pk: int, slate: str | None = None) -> str:
    """Classifies a game for grading a row logged for `slate`: 'final' | 'no_contest' | 'pending'.

    Box scores populate live (and pre-game), so player stats MUST NOT be graded
    until this returns 'final' — otherwise a batter sitting on 0 hits in the 3rd
    inning gets permanently recorded as a loss.

    The entry for the row's OWN slate decides. A row logged for a date the game was then
    postponed from is void, as the book voids that bet, even once the makeup is played;
    a row logged on the makeup date grades on the makeup. Reading whichever entry came
    first voided all 12 replay predictions for 849817 (postponed 10-16, played 10-18). A
    suspended entry defers to the game's latest one, which is where it resumes. With no
    slate, or none that matches, the latest entry decides.
    """
    key = (game_pk, slate)
    if key in _GAME_STATE_CACHE:
        return _GAME_STATE_CACHE[key]
    entries = _fetch_game_entries(game_pk)
    if not entries:
        return "pending"
    entry = next((e for e in entries if e["slate"] == slate), None)
    if entry is None or "suspended" in entry["detailed"].lower():
        entry = entries[-1]

    low = entry["detailed"].lower()
    if any(k in low for k in _NO_CONTEST_STATES):
        state = "no_contest"     # never played — void, not a loss
    elif "suspended" in low:
        state = "pending"        # resumes later
    else:
        state = "final" if entry["abstract"] == "Final" else "pending"
    if state != "pending":
        _GAME_STATE_CACHE[key] = state
    return state


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
