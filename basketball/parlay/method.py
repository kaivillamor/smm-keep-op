"""
The owner's parlay method. Paper only: nothing here places a bet.

Each slate: score every FanDuel points, rebounds and assists leg (main lines, plus points
rungs 2-5 under), keep the legs that pass fails(), build the best $10 parlay and then the
best $5 from the players it left, and store both for grading.
"""
from itertools import combinations
from math import prod

# ── the owner's settings (rules quoted in METHOD.md) ─────────────────────────────

METHOD_MARKETS = ("player_points", "player_rebounds", "player_assists")
POINTS_UNDERCUT = (2.0, 5.0)   # points rungs this far under the main line
TRUST_MINUTES = 28.0
SIXTH_MAN_MINUTES = 20.0
SIXTH_MAN_MARKETS = ("player_points", "player_assists")
CONSISTENCY_L10 = 0.7
CONSISTENCY_L20 = 0.6

H2H_SEASONS = 3                # this season and the two before it
H2H_MIN_GAMES = 2
SIMILAR_TEAMS = 5              # nearest defenses by what they allow in this market
SIMILAR_MIN_GAMES = 3
if H2H_SEASONS < 2:
    raise ValueError("H2H_SEASONS counts this season and last at least: the game log has both")

STINGY_VS_POSITION = 3         # skip opponents ranked 1..this against his position

# Keyed by ESPN id, never by name; a team ban drops all its players:
#     BANNED_PLAYERS = {"<player id>": ("Name", "date added", "why"), ...}
#     BANNED_TEAMS   = {"<team id>": ("ABBR", "date added", "why"), ...}
BANNED_PLAYERS: dict[str, tuple] = {}
BANNED_TEAMS: dict[str, tuple] = {}

MAX_FAVORITE = -400
SWEETENER_MIN_GAIN = 20.0
LEG_COUNTS = (3, 4, 5)
TIERS = {
    "$5":  {"stake": 5.0,  "profit": (30.0, 70.0),   "min_l10": 0.7},
    "$10": {"stake": 10.0, "profit": (100.0, 300.0), "min_l10": 0.8},
}
BUILD_ORDER = ("$10", "$5")
if sorted(BUILD_ORDER) != sorted(TIERS):
    raise ValueError(f"BUILD_ORDER {BUILD_ORDER} must name every tier in TIERS once")

# Search bounds: POOL_CAP 30 keeps a 3-5 leg search to ~170k combinations.
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


def over(vals: list, line: float | None) -> int | None:
    """How many of these games cleared `line`."""
    return None if line is None else sum(1 for v in vals if v > line)


def more_than_half(hits: int, games: int) -> bool:
    """The owner's head-to-head and similar-defenses bar: 3 of 5 passes, 2 of 4 does not."""
    return 2 * hits > games


def mean(vals: list) -> float | None:
    """Mean to 2 places; None if empty."""
    return round(sum(vals) / len(vals), 2) if vals else None


def meetings(rows: list, stat: str, opponent_id: str | None) -> list:
    """His `stat` in every game against tonight's opponent, newest first. `rows` spans
    H2H_SEASONS seasons."""
    return [g[stat] for g in rows
            if opponent_id and g.get("opponent_id") == opponent_id and g.get(stat) is not None]


def similar_opponent_games(rows: list, stat: str, opponent_id: str, factors: dict,
                           team_stat: str, k: int = SIMILAR_TEAMS) -> list:
    """His `stat` against the k defenses closest to tonight's opponent in what they allow
    in this market, tonight's opponent excluded."""
    f0 = factors.get((opponent_id, team_stat))
    if f0 is None:
        return []
    nearest = sorted((abs(f - f0), t) for (t, s), f in factors.items()
                     if s == team_stat and t != opponent_id)
    similar = {t for _, t in nearest[:k]}
    return [g[stat] for g in rows if g.get("opponent_id") in similar and g.get(stat) is not None]


def player_inputs(rows: list, stat: str, opponent_id: str | None) -> dict:
    """Minutes, averages and head-to-head, read straight from the game log: never from
    player_form(). Season average is this season's games, or last season's before the first.
    """
    avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    this = [g[stat] for g in rows if not g.get("prior") and g.get(stat) is not None]
    last = [g[stat] for g in rows if g.get("prior") and g.get(stat) is not None]
    recent = [g[stat] for g in rows[:10] if g.get(stat) is not None]
    mins = ([g["minutes"] for g in rows[:10] if g.get("minutes") is not None]
            or [g["minutes"] for g in rows if g.get("minutes") is not None])
    h2h = [g[stat] for g in rows
           if opponent_id and g.get("opponent_id") == opponent_id and g.get(stat) is not None]
    return {"minutes_avg": avg(mins), "season_avg": avg(this or last),
            "recent_avg": avg(recent), "h2h_avg": avg(h2h), "h2h_games": len(h2h)}


def legs_for(candidate: dict, rows: list, stat: str, rungs: list, own: dict,
             similar: list, meet: list) -> list[dict]:
    """One leg per rung for a player-market, with every input the rules read.

    `rungs` is [(line, odds, undercut), ...] — the main line first (undercut 0). `own` is
    player_inputs() for this player and market; `similar` and `meet` are his games against
    the similar defenses and against tonight's opponent, counted at each rung's own line.
    """
    legs = []
    for line, odds, undercut in rungs:
        r10, h10, n10 = hit_rate(rows, stat, line, 10)
        r20, h20, n20 = hit_rate(rows, stat, line, 20)
        # Scoring uses the smoothed rate; the threshold checks use the raw one.
        smooth = (h10 + 1) / (n10 + 2) if n10 else None
        legs.append({
            **{k: candidate.get(k) for k in ("game_id", "player_id", "player_name", "team",
                                             "team_id", "opponent", "tipoff", "injury_status",
                                             "position", "opp_pos_rank")},
            "market": candidate["market"], "line": line,
            "main_line": candidate.get("line"), "undercut": undercut,
            "odds": odds, "decimal": decimal(odds), "implied": implied(odds),
            "hit_l10": r10, "hits_l10": h10, "games_l10": n10,
            "hit_l20": r20, "hits_l20": h20, "games_l20": n20,
            "smoothed": smooth,
            "worth": (smooth / implied(odds)) if smooth else None,
            "minutes_avg": own["minutes_avg"],
            "season_avg": own["season_avg"], "recent_avg": own["recent_avg"],
            "h2h_avg": own["h2h_avg"], "h2h_games": own["h2h_games"],
            "h2h_hits": over(meet, line), "h2h_meetings": len(meet),
            "sim_avg": mean(similar), "sim_games": len(similar),
            "sim_hits": over(similar, line),
            "teammate_usage_out": candidate.get("teammate_usage_out"),
            "sweetener": odds <= MAX_FAVORITE,
        })
    return legs


# ── the rules ────────────────────────────────────────────────────────────────────

def untrusted(minutes: float | None, market: str) -> str | None:
    """None if the player is trusted in this market, else why not. web/app.py restates
    this as NBA_TRUSTED_SQL: change both."""
    minutes = minutes or 0
    if market not in METHOD_MARKETS:
        return "not_a_method_market"
    if minutes >= TRUST_MINUTES:
        return None
    if minutes >= SIXTH_MAN_MINUTES:
        return None if market in SIXTH_MAN_MARKETS else "sixth_man_market"
    return "not_trusted_minutes"


def banned(player_id, team_id) -> str | None:
    """'player' or 'team' if a ban list covers this player, else None."""
    if str(player_id) in BANNED_PLAYERS:
        return "player"
    if str(team_id) in BANNED_TEAMS:
        return "team"
    return None


def fails(leg: dict) -> str | None:
    """None if the leg passes the owner's rules, else the first rule it fails."""
    line = leg["line"]
    ban = banned(leg["player_id"], leg.get("team_id"))
    if ban:
        return f"banned_{ban}"
    if leg.get("injury_status") != "active":
        return "not_confirmed_active"     # out, day-to-day, or no report to check against
    why = untrusted(leg.get("minutes_avg"), leg["market"])
    if why:
        return why
    if leg["games_l10"] < 10 or leg["games_l20"] < 20:
        return "short_history"
    if leg["hit_l10"] < CONSISTENCY_L10 or leg["hit_l20"] < CONSISTENCY_L20:
        return "inconsistent"
    if (leg.get("season_avg") or 0) <= line or (leg.get("recent_avg") or 0) <= line:
        return "average_below_line"
    if (leg["h2h_meetings"] >= H2H_MIN_GAMES
            and not more_than_half(leg["h2h_hits"], leg["h2h_meetings"])):
        return "head_to_head_under_line"
    if (leg["sim_games"] >= SIMILAR_MIN_GAMES
            and not more_than_half(leg["sim_hits"], leg["sim_games"])):
        return "similar_defenses_under_line"
    # No rank (backfill unfinished, or no ESPN position): the rule does not apply.
    if leg.get("opp_pos_rank") is not None and leg["opp_pos_rank"] <= STINGY_VS_POSITION:
        return "stingy_vs_position"
    if not leg["worth"] or leg["worth"] < 1.0:
        return "price_too_short"          # smoothed record must beat the implied price
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

def build(legs: list[dict], tier: str, taken: frozenset = frozenset()) -> dict | None:
    """The best parlay for a tier, or None if nothing fits its targets. Best = highest
    product of the legs' smoothed hit rates, ties to the bigger payout. Players in `taken`
    are skipped."""
    t = TIERS[tier]
    stake, (lo, hi) = t["stake"], t["profit"]
    pool = sorted((l for l in legs
                   if l["hit_l10"] >= t["min_l10"] and l["player_id"] not in taken),
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
            # A sweetener (MAX_FAVORITE or shorter) must add SWEETENER_MIN_GAIN to the payout.
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


def build_slate(legs: list[dict], held: dict[str, set]) -> tuple[dict, dict]:
    """Tonight's parlay per open tier, built in BUILD_ORDER with no player in two slips.

    `held` is frozen(): locked tiers, whose players are out for every tier. Also returns
    how many players each tier was built without.
    """
    taken = set().union(*held.values())
    parlays, held_out = {}, {}
    for tier in BUILD_ORDER:
        if tier in held:
            continue
        held_out[tier] = len(taken)
        parlays[tier] = build(legs, tier, frozenset(taken))
        if parlays[tier]:
            taken |= {l["player_id"] for l in parlays[tier]["legs"]}
    return parlays, held_out


# ── storage ──────────────────────────────────────────────────────────────────────

_LEG_COLS = ("game_id", "player_id", "player_name", "team", "opponent", "tipoff", "market",
             "line", "main_line", "undercut", "odds", "hit_l10", "hit_l20", "minutes_avg",
             "season_avg", "recent_avg", "h2h_avg", "h2h_games", "sim_avg", "sim_games",
             "teammate_usage_out", "h2h_hits", "h2h_meetings", "sim_hits", "position",
             "opp_pos_rank")


def frozen(conn, day: str, now: str) -> dict[str, set[str]]:
    """This slate's frozen tiers (first leg tipped) and each one's players."""
    held: dict[str, set[str]] = {}
    for pid, tier in conn.execute(
            "SELECT id, tier FROM method_parlays WHERE date=? AND first_tip <= ?",
            (day, now)).fetchall():
        held.setdefault(tier, set()).update(str(p) for (p,) in conn.execute(
            "SELECT player_id FROM method_legs WHERE parlay_id=?", (pid,)))
    return held


def save(conn, day: str, parlays: dict, now: str) -> list[str]:
    """Store tonight's parlay per tier; returns a status line per tier. Each run replaces
    (or, if nothing fits, removes) a tier's parlay until its first leg tips, then it is
    frozen."""
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
    """Grade pending legs, then each parlay whose legs are all settled. A void leg drops
    out and the parlay pays on the rest; any losing leg loses it."""
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

_MARKET_WORD = {"player_points": "points", "player_rebounds": "rebounds",
                "player_assists": "assists"}


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
                sim, simn, outc, h2h_hits, h2h_meets, sim_hits, pos, pos_rank) in enumerate(
                conn.execute(
                "SELECT player_name, team, opponent, market, line, main_line, undercut, odds, "
                "hit_l10, hit_l20, h2h_avg, h2h_games, sim_avg, sim_games, outcome, "
                "h2h_hits, h2h_meetings, sim_hits, position, opp_pos_rank "
                "FROM method_legs WHERE parlay_id=? ORDER BY id", (pid,)), 1):
            rung = "main line" if not cut else f"{cut:g} under {main:g}"
            # Legs stored before 10-29 have only the averages the old rules read.
            matchup = []
            if h2h_meets:
                matchup.append(f"h2h {h2h_hits}/{h2h_meets}")
            elif h2hn and h2h_meets is None:
                matchup.append(f"h2h {h2h:.1f} ({h2hn})")
            if simn:
                matchup.append(f"similar D {sim_hits}/{simn}" if sim_hits is not None
                               else f"similar D {sim:.1f} ({simn})")
            if pos_rank:
                matchup.append(f"vs {pos} #{pos_rank}")
            out.append(f"  {i}. {name} ({team} vs {opp})  {_MARKET_WORD.get(market, market)} "
                       f"over {line:g}  {odds:+d}  [{rung}]  {round(l10 * 10)}/10, "
                       f"{round(l20 * 20)}/20   {'  '.join(matchup)}"
                       f"{'   -> ' + outc if outc else ''}")
    return "\n".join(out) if out else f"No method parlays stored for {day}."
