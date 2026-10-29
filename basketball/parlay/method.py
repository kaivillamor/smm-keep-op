"""
The owner's parlay method, written down so the system can follow it and keep score.

Written 2026-10-26 from the owner's own description, BEFORE measuring it — the honest-use
rule from model/factors/owner_logic.py applies here too: write the method down first and
measure it forward. Retuning these settings after reading the method's results would fit
noise and the record would stop being evidence. Change a setting because the method
changed, never because last week's parlays lost.

What it does each slate:
  1. score every FanDuel points, rebounds and assists leg the owner would consider — main
     lines, plus points rungs 2-5 under the main line;
  2. keep the legs that pass the owner's rules (bans, trust, consistency, average,
     head-to-head, similar defenses, defense vs position, price);
  3. build the best $10 parlay, then the best $5 from the players it left, each inside the
     owner's payout targets;
  4. store them, so every one is graded against the box scores.

Paper only. Nothing here places a bet.
"""
from itertools import combinations
from math import prod

# ── the owner's settings, in the owner's words ──────────────────────────────────

# "For rebounds we check the head to head and their consistency." Points and rebounds are
# the markets the method was first described for. Assists joined 2026-10-27, for starters
# and sixth men alike, at the owner's word. Threes are not part of it.
METHOD_MARKETS = ("player_points", "player_rebounds", "player_assists")

# "take 5 points under ... It's even better if you can find alt lines where I undercut by
# 4 points and 2 points if the odds are good." Every FanDuel rung 2-5 points under the main
# line, points only. 3 is included because FanDuel ladders mostly run in 1-point steps.
POINTS_UNDERCUT = (2.0, 5.0)

# "star-players or players I trust will be in the starting lineup or will be in down the
# stretch in the 4th quarter." Recent minutes is the measurable stand-in: 28+ a game is a
# starter or a closer. ESPN's free data has no quarter-by-quarter minutes.
TRUST_MINUTES = 28.0

# "You're better off judging sixth man's by their points or assists in terms of
# consistency." A bench player on 20+ minutes is trusted in those two markets only, and
# still has to pass every rule below. Under 20 is not a sixth man (the owner, 2026-10-27).
SIXTH_MAN_MINUTES = 20.0
SIXTH_MAN_MARKETS = ("player_points", "player_assists")

# "The main thing I look for is consistency." Cleared this exact line in 7+ of the last 10
# games and 12+ of the last 20.
CONSISTENCY_L10 = 0.7
CONSISTENCY_L20 = 0.6

# "how they perform against teams similar to their opponent or against their opponent in
# general which is just head2head." Applied only with enough games to mean anything.
# Since 2026-10-29 both count the way consistency does: how often he cleared THIS line in
# those games, which has to be more than half (the owner's bar). Until then each compared an
# average to the line, so one big night hid the misses: Saddiq Bey's 10.0 against DEN beat
# a 9.5 line on 2 overs in 4. Head-to-head also looks back three seasons, not two, which
# left one to four meetings: Alexander-Walker was 3 of 3 against HOU inside the window and
# 0 of 4 the season before it. Similar defenses stays on two seasons, because the defenses
# are matched on what they allow now.
H2H_SEASONS = 3                # this season and the two before it
H2H_MIN_GAMES = 2
SIMILAR_TEAMS = 5              # nearest defenses by what they allow in this market
SIMILAR_MIN_GAMES = 3
if H2H_SEASONS < 2:
    raise ValueError("H2H_SEASONS counts this season and last at least: the game log has both")

# "Teams that play solid defense or have an opponent team player set to match up against
# our intended player" (the owner, 2026-10-29). ESPN has no data on who guards whom, so the
# measure is what tonight's opponent allows to his position (guard, forward or center) per
# 36 minutes in this market, from stored box scores (pipeline/team_defense). The owner
# skips the 3 stingiest of 30.
STINGY_VS_POSITION = 3

# "We also need to begin banning teams/players" (the owner, 2026-10-29). A banned player is
# never used; a banned team means none of its players are. Both filter this method only:
# banned players keep their research rows, and every row logs whether it was banned, so the
# bans can be measured (output/research.compare_bans). Keyed by ESPN id, never by name:
#     BANNED_PLAYERS = {"<player id>": ("Name", "date added", "why"), ...}
#     BANNED_TEAMS   = {"<team id>": ("ABBR", "date added", "why"), ...}
BANNED_PLAYERS: dict[str, tuple] = {}
BANNED_TEAMS: dict[str, tuple] = {}

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

# "The standard should be taking different legs or different players." (the owner,
# 2026-10-28, after the $5 and $10 slips shared four of five players on 10-27 and 10-28.)
# No player sits in both slips, at any line or market. The $10 picks first: its 8-of-10
# bar leaves it the smaller pool, and the $5 builds from the players it left.
BUILD_ORDER = ("$10", "$5")
if sorted(BUILD_ORDER) != sorted(TIERS):
    raise ValueError(f"BUILD_ORDER {BUILD_ORDER} must name every tier in TIERS once")

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


def over(vals: list, line: float | None) -> int | None:
    """How many of these games cleared `line`."""
    return None if line is None else sum(1 for v in vals if v > line)


def more_than_half(hits: int, games: int) -> bool:
    """The owner's head-to-head and similar-defenses bar: 3 of 5 passes, 2 of 4 does not."""
    return 2 * hits > games


def mean(vals: list) -> float | None:
    """To 2 places, as sim_avg and sim_opp_avg have always been stored."""
    return round(sum(vals) / len(vals), 2) if vals else None


def meetings(rows: list, stat: str, opponent_id: str | None) -> list:
    """His `stat` in every game against tonight's opponent, newest first. `rows` carries
    H2H_SEASONS seasons here: the game log plus the older seasons main.py fetches apart."""
    return [g[stat] for g in rows
            if opponent_id and g.get("opponent_id") == opponent_id and g.get(stat) is not None]


def similar_opponent_games(rows: list, stat: str, opponent_id: str, factors: dict,
                           team_stat: str, k: int = SIMILAR_TEAMS) -> list:
    """His `stat` in every game against the k defenses closest to tonight's opponent in what
    they allow in this market — tonight's opponent itself excluded, since head-to-head is
    measured apart."""
    f0 = factors.get((opponent_id, team_stat))
    if f0 is None:
        return []
    nearest = sorted((abs(f - f0), t) for (t, s), f in factors.items()
                     if s == team_stat and t != opponent_id)
    similar = {t for _, t in nearest[:k]}
    return [g[stat] for g in rows if g.get("opponent_id") in similar and g.get(stat) is not None]


def player_inputs(rows: list, stat: str, opponent_id: str | None) -> dict:
    """Minutes, averages and head-to-head, read straight from the game log.

    The method's own reads, never the model's player_form(), so tuning the model cannot
    change which legs this method takes. It did once: from 10-26 player_form() shrank this
    season toward last season, and the average rule read that blend (about three quarters
    last season, five games in), though nothing in the owner's description asks for one.
    Season average here is this season's games alone, or last season's before the first.
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
        # The rate used for SCORING is smoothed — (hits+1)/(games+2) — so a perfect 10 of
        # 10 ranks as very likely, not certain. The threshold checks use the raw rate,
        # because "7 of the last 10" is how the owner states the rule.
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
    """None if the owner trusts this player in this market, else why not.

    Starters and closers in every method market, sixth men in points and assists. The
    research report reads this too, so its "trusted" cut cannot drift from the method's.
    """
    minutes = minutes or 0
    if market not in METHOD_MARKETS:
        return "not_a_method_market"
    if minutes >= TRUST_MINUTES:
        return None
    if minutes >= SIXTH_MAN_MINUTES:
        return None if market in SIXTH_MAN_MARKETS else "sixth_man_market"
    return "not_trusted_minutes"


def banned(player_id, team_id) -> str | None:
    """'player' or 'team' if a ban list covers this player, else None. The rule and the
    research log both read this, so the flag can never disagree with the filter."""
    if str(player_id) in BANNED_PLAYERS:
        return "player"
    if str(team_id) in BANNED_TEAMS:
        return "team"
    return None


def fails(leg: dict) -> str | None:
    """None if the leg passes the owner's rules, else the first rule it fails.

    A reason rather than a bare False: a night where nothing qualifies has to say why, or
    it is indistinguishable from a broken filter (§29).
    """
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
    # No rank (the box-score backfill is unfinished, or ESPN lists no position): the rule
    # cannot judge, so it does not apply. The collector prints how many legs that was.
    if leg.get("opp_pos_rank") is not None and leg["opp_pos_rank"] <= STINGY_VS_POSITION:
        return "stingy_vs_position"
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

def build(legs: list[dict], tier: str, taken: frozenset = frozenset()) -> dict | None:
    """The best parlay for a tier, or None if nothing fits the owner's targets.

    Best = highest product of the legs' smoothed hit rates — a RANKING score built from
    the legs' own records, not a calibrated probability. Ties go to the bigger payout.
    `taken` is the players already in another of tonight's slips; none of them is used.
    """
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


def build_slate(legs: list[dict], held: dict[str, set]) -> tuple[dict, dict]:
    """Tonight's parlay per tier still open, with no player in two slips.

    Tiers build in BUILD_ORDER, each clear of every player an earlier tier took. `held` is
    frozen(): tiers already locked at tip, which save() keeps as they are, so their players
    are out for every tier: a slip that could have been placed outranks first pick. Also
    returns how many players each tier was built without, so a tier that comes back empty
    can say whether the rules left it nothing or the other slip did.
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
    """This slate's frozen tiers (first leg tipped, save()'s test), with each one's players,
    so the tiers still being built can stay clear of them."""
    held: dict[str, set[str]] = {}
    for pid, tier in conn.execute(
            "SELECT id, tier FROM method_parlays WHERE date=? AND first_tip <= ?",
            (day, now)).fetchall():
        held.setdefault(tier, set()).update(str(p) for (p,) in conn.execute(
            "SELECT player_id FROM method_legs WHERE parlay_id=?", (pid,)))
    return held


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
