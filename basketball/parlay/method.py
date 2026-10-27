"""
The owner's parlay method, written down so the system can follow it and keep score.

Written 2026-10-26 from the owner's own description, BEFORE measuring it — the honest-use
rule from model/factors/owner_logic.py applies here too: write the method down first and
measure it forward. Retuning these settings after reading the method's results would fit
noise and the record would stop being evidence. Change a setting because the method
changed, never because last week's parlays lost.

What it does each slate:
  1. score every FanDuel points and rebounds leg the owner would consider — main lines,
     plus points rungs 2-5 under the main line;
  2. keep the legs that pass the owner's rules (trust, consistency, average, matchup,
     price);
  3. build the best $5 and $10 parlays inside the owner's payout targets;
  4. store them, so every one is graded against the box scores.

Paper only. Nothing here places a bet.
"""
from itertools import combinations
from math import prod

# ── the owner's settings, in the owner's words ──────────────────────────────────

# "For rebounds we check the head to head and their consistency." Points and rebounds are
# the markets the method was described for; assists and threes are not part of it.
METHOD_MARKETS = ("player_points", "player_rebounds")

# "take 5 points under ... It's even better if you can find alt lines where I undercut by
# 4 points and 2 points if the odds are good." Every FanDuel rung 2-5 points under the main
# line, points only. 3 is included because FanDuel ladders mostly run in 1-point steps.
POINTS_UNDERCUT = (2.0, 5.0)

# "star-players or players I trust will be in the starting lineup or will be in down the
# stretch in the 4th quarter." Recent minutes is the measurable stand-in: 28+ a game is a
# starter or a closer. ESPN's free data has no quarter-by-quarter minutes.
TRUST_MINUTES = 28.0

# "The main thing I look for is consistency." Cleared this exact line in 7+ of the last 10
# games and 12+ of the last 20.
CONSISTENCY_L10 = 0.7
CONSISTENCY_L20 = 0.6

# "how they perform against teams similar to their opponent or against their opponent in
# general which is just head2head." Applied only with enough games to mean anything.
H2H_MIN_GAMES = 2
SIMILAR_TEAMS = 5              # nearest defenses by what they allow in this market
SIMILAR_MIN_GAMES = 3

# "I tend to avoid anything that's -400 over but if it adds a significant amount (+$20)
# to the parlay payout i keep it."
MAX_FAVORITE = -400
SWEETENER_MIN_GAIN = 20.0

# "I generally keep my parlays within 3 to 5 legs now."
LEG_COUNTS = (3, 4, 5)

# "I usually stake $5 for a small profit of anything from 30 to 70 dollars and for $10 i
# aim for anything over $100 to $300 if the odds are really good and the stats look good."
# "Stats look good" for the $10 tier = every leg cleared its line 8+ of the last 10.
TIERS = {
    "$5":  {"stake": 5.0,  "profit": (30.0, 70.0),   "min_l10": 0.7},
    "$10": {"stake": 10.0, "profit": (100.0, 300.0), "min_l10": 0.8},
}

# Search bounds. Legs are ranked first, so the cap only drops the weakest candidates; 30
# keeps a 3-5 leg search to ~170k combinations, well under a second.
RUNGS_PER_PLAYER_MARKET = 2
POOL_CAP = 30


# ── odds ─────────────────────────────────────────────────────────────────────────

def decimal(odds: int) -> float:
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / abs(odds))


def implied(odds: int) -> float:
    return 1.0 / decimal(odds)


def to_american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2.0 else round(-100 / (dec - 1))


# ── features ─────────────────────────────────────────────────────────────────────

def hit_rate(rows: list, stat: str, line: float, n: int) -> tuple[float | None, int, int]:
    """(rate, hits, games) clearing `line` over the newest `n` games with the stat."""
    vals = [g[stat] for g in rows if g.get(stat) is not None][:n]
    if not vals:
        return None, 0, 0
    hits = sum(1 for v in vals if v > line)
    return hits / len(vals), hits, len(vals)


def similar_opponent_avg(rows: list, stat: str, opponent_id: str, factors: dict,
                         team_stat: str, k: int = SIMILAR_TEAMS) -> tuple[float | None, int]:
    """Average vs the k defenses closest to tonight's opponent in what they allow in this
    market — tonight's opponent itself excluded, since head-to-head is measured apart."""
    f0 = factors.get((opponent_id, team_stat))
    if f0 is None:
        return None, 0
    nearest = sorted((abs(f - f0), t) for (t, s), f in factors.items()
                     if s == team_stat and t != opponent_id)
    similar = {t for _, t in nearest[:k]}
    vals = [g[stat] for g in rows if g.get("opponent_id") in similar and g.get(stat) is not None]
    return (round(sum(vals) / len(vals), 2) if vals else None), len(vals)


def legs_for(candidate: dict, rows: list, stat: str, rungs: list, form: dict,
             h2h: tuple, similar: tuple) -> list[dict]:
    """One leg per rung for a player-market, with every input the rules read.

    `rungs` is [(line, odds, undercut), ...] — the main line first (undercut 0).
    """
    legs = []
    for line, odds, undercut in rungs:
        r10, h10, n10 = hit_rate(rows, stat, line, 10)
        r20, h20, n20 = hit_rate(rows, stat, line, 20)
        # The rate used for SCORING is smoothed — (hits+1)/(games+2) — so a perfect 10 of
        # 10 ranks as very likely, not certain. The threshold checks use the raw rate,
        # because "7 of the last 10" is how the owner states the rule.
        smooth = (h10 + 1) / (n10 + 2) if n10 else None
        legs.append({
            **{k: candidate.get(k) for k in ("game_id", "player_id", "player_name", "team",
                                             "opponent", "tipoff", "injury_status")},
            "market": candidate["market"], "line": line,
            "main_line": candidate.get("line"), "undercut": undercut,
            "odds": odds, "decimal": decimal(odds), "implied": implied(odds),
            "hit_l10": r10, "hits_l10": h10, "games_l10": n10,
            "hit_l20": r20, "hits_l20": h20, "games_l20": n20,
            "smoothed": smooth,
            "worth": (smooth / implied(odds)) if smooth else None,
            "minutes_avg": form.get("minutes_avg"),
            "season_avg": form.get("season_avg"), "recent_avg": form.get("recent_avg"),
            "h2h_avg": h2h[0], "h2h_games": h2h[1],
            "sim_avg": similar[0], "sim_games": similar[1],
            "teammate_usage_out": candidate.get("teammate_usage_out"),
            "sweetener": odds <= MAX_FAVORITE,
        })
    return legs


# ── the rules ────────────────────────────────────────────────────────────────────

def fails(leg: dict) -> str | None:
    """None if the leg passes the owner's rules, else the first rule it fails.

    A reason rather than a bare False: a night where nothing qualifies has to say why, or
    it is indistinguishable from a broken filter (§29).
    """
    line = leg["line"]
    if leg.get("injury_status") != "active":
        return "not_confirmed_active"     # out, day-to-day, or no report to check against
    if (leg.get("minutes_avg") or 0) < TRUST_MINUTES:
        return "not_trusted_minutes"
    if leg["games_l10"] < 10 or leg["games_l20"] < 20:
        return "short_history"
    if leg["hit_l10"] < CONSISTENCY_L10 or leg["hit_l20"] < CONSISTENCY_L20:
        return "inconsistent"
    if (leg.get("season_avg") or 0) <= line or (leg.get("recent_avg") or 0) <= line:
        return "average_below_line"
    if leg["h2h_games"] >= H2H_MIN_GAMES and (leg["h2h_avg"] or 0) <= line:
        return "head_to_head_below_line"
    if leg["sim_games"] >= SIMILAR_MIN_GAMES and (leg["sim_avg"] or 0) <= line:
        return "similar_defenses_below_line"
    if not leg["worth"] or leg["worth"] < 1.0:
        return "price_too_short"          # "if the odds are good": record must beat price
    return None


def qualify(legs: list[dict]) -> tuple[list[dict], dict]:
    """Passing legs (best rungs per player-market) and a count of why the rest failed."""
    reasons: dict[str, int] = {}
    passing: dict[tuple, list] = {}
    for leg in legs:
        why = fails(leg)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        passing.setdefault((leg["player_id"], leg["market"]), []).append(leg)
    kept = []
    for rungs in passing.values():
        rungs.sort(key=lambda l: l["worth"], reverse=True)
        kept.extend(rungs[:RUNGS_PER_PLAYER_MARKET])
    return kept, reasons


# ── building ─────────────────────────────────────────────────────────────────────

def build(legs: list[dict], tier: str) -> dict | None:
    """The best parlay for a tier, or None if nothing fits the owner's targets.

    Best = highest product of the legs' smoothed hit rates — a RANKING score built from
    the legs' own records, not a calibrated probability. Ties go to the bigger payout.
    """
    t = TIERS[tier]
    stake, (lo, hi) = t["stake"], t["profit"]
    pool = sorted((l for l in legs if l["hit_l10"] >= t["min_l10"]),
                  key=lambda l: (l["smoothed"], l["worth"]), reverse=True)[:POOL_CAP]
    best, best_key = None, None
    for n in LEG_COUNTS:
        for combo in combinations(pool, n):
            if len({l["player_id"] for l in combo}) < n:
                continue                       # one leg per player
            dec = prod(l["decimal"] for l in combo)
            profit = stake * (dec - 1)
            if not lo <= profit <= hi:
                continue
            # A -400-or-shorter leg stays only if it adds $20+ to the payout.
            if any(stake * (dec / l["decimal"]) * (l["decimal"] - 1) < SWEETENER_MIN_GAIN
                   for l in combo if l["sweetener"]):
                continue
            key = (prod(l["smoothed"] for l in combo), profit)
            if best_key is None or key > best_key:
                best, best_key = combo, key
    if best is None:
        return None
    dec = prod(l["decimal"] for l in best)
    return {
        "tier": tier, "stake": stake, "legs": list(best), "decimal_odds": round(dec, 4),
        "american_odds": to_american(dec), "profit_if_win": round(stake * (dec - 1), 2),
        "record_score": round(best_key[0], 4),
        "first_tip": min((l["tipoff"] or "") for l in best) or None,
    }


# ── storage ──────────────────────────────────────────────────────────────────────

_LEG_COLS = ("game_id", "player_id", "player_name", "team", "opponent", "tipoff", "market",
             "line", "main_line", "undercut", "odds", "hit_l10", "hit_l20", "minutes_avg",
             "season_avg", "recent_avg", "h2h_avg", "h2h_games", "sim_avg", "sim_games",
             "teammate_usage_out")


def save(conn, day: str, parlays: dict, now: str) -> list[str]:
    """Store tonight's parlay per tier. Returns a status line per tier.

    One parlay per tier per slate, replaced on each run — UNTIL its first leg tips. From
    then on it is frozen, because that is the version that could actually have been
    placed. A run that finds nothing for an unfrozen tier removes the earlier pick: the
    record is what the method said at its last chance before tip, not its first guess.
    """
    status = []
    for tier in TIERS:
        old = conn.execute("SELECT id, first_tip FROM method_parlays WHERE date=? AND tier=?",
                           (day, tier)).fetchone()
        if old and old[1] and old[1] <= now:
            status.append(f"{tier}: frozen — first leg has tipped")
            continue
        if old:
            conn.execute("DELETE FROM method_legs WHERE parlay_id=?", (old[0],))
            conn.execute("DELETE FROM method_parlays WHERE id=?", (old[0],))
        p = parlays.get(tier)
        if not p:
            status.append(f"{tier}: nothing fits the targets")
            continue
        cur = conn.execute(
            "INSERT INTO method_parlays (date, tier, stake, n_legs, decimal_odds, "
            "american_odds, profit_if_win, record_score, first_tip, generated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (day, tier, p["stake"], len(p["legs"]), p["decimal_odds"], p["american_odds"],
             p["profit_if_win"], p["record_score"], p["first_tip"], now))
        for leg in p["legs"]:
            conn.execute(
                f"INSERT INTO method_legs (parlay_id, {', '.join(_LEG_COLS)}) "
                f"VALUES (?, {', '.join('?' * len(_LEG_COLS))})",
                (cur.lastrowid, *(leg.get(c) for c in _LEG_COLS)))
        status.append(f"{tier}: {len(p['legs'])} legs, {p['american_odds']:+d}, "
                      f"${p['profit_if_win']:.2f} profit on ${p['stake']:.0f}")
    conn.commit()
    return status


# ── grading ──────────────────────────────────────────────────────────────────────

def grade(conn, finals: dict, called_off: set, box_for) -> int:
    """Grade pending legs, then any parlay whose legs are all settled.

    A void leg (did not play, or the game was called off) drops out and the parlay pays
    on the legs that remain — how a book reprices it. Any losing leg loses the parlay.
    """
    from pipeline.stats_fetcher import MARKET_STAT

    pending = conn.execute(
        "SELECT id, game_id, player_id, market, line FROM method_legs WHERE outcome IS NULL"
    ).fetchall()
    boxes: dict[str, dict] = {}
    graded = 0
    for leg_id, game_id, player_id, market, line in pending:
        gid = str(game_id)
        if gid in called_off:
            conn.execute("UPDATE method_legs SET outcome='void' WHERE id=?", (leg_id,))
            graded += 1
            continue
        if not finals.get(gid):
            continue
        if gid not in boxes:
            boxes[gid] = box_for(gid)
        player = boxes[gid].get(str(player_id))
        actual = None if player is None else player.get(MARKET_STAT.get(market))
        outcome = "void" if actual is None else ("win" if actual > float(line) else "loss")
        conn.execute("UPDATE method_legs SET outcome=?, actual_value=? WHERE id=?",
                     (outcome, actual, leg_id))
        graded += 1

    for pid, stake in conn.execute(
            "SELECT id, stake FROM method_parlays WHERE outcome IS NULL").fetchall():
        legs = conn.execute("SELECT outcome, odds FROM method_legs WHERE parlay_id=?",
                            (pid,)).fetchall()
        if any(o is None for o, _ in legs):
            continue
        live = [odds for o, odds in legs if o != "void"]
        if any(o == "loss" for o, _ in legs):
            outcome, profit = "loss", -stake
        elif not live:
            outcome, profit = "void", 0.0
        else:
            outcome, profit = "win", round(stake * (prod(decimal(o) for o in live) - 1), 2)
        conn.execute("UPDATE method_parlays SET outcome=?, profit=? WHERE id=?",
                     (outcome, profit, pid))
    conn.commit()
    return graded


# ── display ──────────────────────────────────────────────────────────────────────

_MARKET_WORD = {"player_points": "points", "player_rebounds": "rebounds"}


def describe(conn, day: str) -> str:
    """Human-readable slip for a slate, for the CLI and the log."""
    out = []
    for pid, tier, n, am, profit, stake, score, gen, outcome, pnl in conn.execute(
            "SELECT id, tier, n_legs, american_odds, profit_if_win, stake, record_score, "
            "generated_at, outcome, profit FROM method_parlays WHERE date=? ORDER BY stake",
            (day,)):
        state = "pending" if outcome is None else f"{outcome} ({pnl:+.2f})"
        out.append(f"{tier} parlay — {n} legs, {am:+d}, ${profit:.2f} profit on "
                   f"${stake:.0f}   [{state}; built {gen[11:16]} UTC]")
        for i, (name, team, opp, market, line, main, cut, odds, l10, l20, h2h, h2hn,
                sim, simn, outc) in enumerate(conn.execute(
                "SELECT player_name, team, opponent, market, line, main_line, undercut, odds, "
                "hit_l10, hit_l20, h2h_avg, h2h_games, sim_avg, sim_games, outcome "
                "FROM method_legs WHERE parlay_id=? ORDER BY id", (pid,)), 1):
            rung = "main line" if not cut else f"{cut:g} under {main:g}"
            matchup = []
            if h2hn:
                matchup.append(f"h2h {h2h:.1f} ({h2hn})")
            if simn:
                matchup.append(f"similar D {sim:.1f} ({simn})")
            out.append(f"  {i}. {name} ({team} vs {opp})  {_MARKET_WORD.get(market, market)} "
                       f"over {line:g}  {odds:+d}  [{rung}]  {round(l10 * 10)}/10, "
                       f"{round(l20 * 20)}/20   {'  '.join(matchup)}"
                       f"{'   -> ' + outc if outc else ''}")
    return "\n".join(out) if out else f"No method parlays stored for {day}."
