"""
NBA prop research collector.

    python main.py --collect        score tonight's priced players and log them
    python main.py --grade          grade finished games from box scores
    python main.py --collect-grade  grade yesterday, then collect tonight
    python main.py --report         calibration + ranking signal
    python main.py --migrate        create/upgrade the research DB schema, nothing else

Research only. Nothing here places or records a bet, and there is deliberately no betting
gate: MLB shipped one before its inputs were logged and it could not fire for three weeks
without anyone noticing (PROJECT.md §29). Collect and measure first.
"""
import sys

from model.factors.owner_logic import apply_prop_owner_logic
from parlay import method
from model.prop_model import prop_probability
from output.research import _connect, grade_predictions, log_predictions, report
from pipeline.calendar import game_slate_date, slate_date
from pipeline.prop_odds import DEFAULT_BOOK, NBA_MARKETS, fetch_prop_board, normalize_name
from pipeline.stats_fetcher import (FETCH_FAILURES, MARKET_STAT, build_player_index, days_rest,
                                    fetch_gamelog, fetch_injuries, fetch_scoreboard,
                                    fetch_season_log, pick_season, player_form, prune_cache,
                                    recent_current_minutes, recent_current_usage)
from pipeline.team_defense import (MARKET_TEAM_STAT, allowance_factors, position_factors,
                                   position_sync_status, sync_player_games, sync_team_games)


def collect(day: str | None = None) -> int:
    """Score every player with a posted line on tonight's slate, and log them all.

    EVERY priced player, not just the ones the model likes. Logging only the surfaced
    picks is a biased sample — the ones the model already rated highly — and it cannot
    answer whether the ranking works at all. That realisation is what made MLB's
    calibration measurable in days instead of months.
    """
    day = day or slate_date()
    print(f"[main] slate {day} — research only, no bets")

    # Scheduled games only. Not `not final` — that includes in-progress games, and
    # re-scoring one replaces a bettable pre-tip line with a live-market number.
    all_games = fetch_scoreboard(day)
    games = [g for g in all_games if not g["started"]]
    if len(games) != len(all_games):
        print(f"[main] skipping {len(all_games) - len(games)} game(s) already underway")
    # ESPN's own season type, per game. The date-based calendar is only a fallback now.
    phase_by_game = {g["game_id"]: g.get("phase") for g in games}
    if not games:
        print("[main] no scheduled games on this slate.")
        return 0
    print(f"[main] {len(games)} game(s)")

    # Index EVERY game on the slate, started ones included, then skip started games
    # explicitly below. Indexing only scheduled games made a player in a game already
    # underway look like a failed name match — 25 of them on 2026-10-26 — and a real
    # matching failure would have been invisible inside that list.
    index = build_player_index(all_games, day, normalize_name)
    scheduled = {g["game_id"] for g in games}
    in_progress: set[str] = set()
    print(f"[main] {len(index)} players on rosters")

    # One game log per PLAYER, reused across all four markets. Fetching per player-market
    # would quadruple the calls for identical data.
    logs: dict[str, tuple] = {}

    # Opponent defense. The first run backfills last season into the DB (~210 calls,
    # once); every run after adds only the dates since.
    season_year = next((g["season_year"] for g in games if g.get("season_year")), None)
    prior_year = season_year - 1 if season_year else None
    factors: dict = {}
    pos_factors: dict = {}
    known_teams: set[str] = set()
    if season_year:
        conn = _connect()
        added = sync_team_games(conn, season_year, day)
        factors = allowance_factors(conn, season_year)
        # Defense vs position, for the method: every player's line from each stored game.
        # The first runs backfill last season a capped batch at a time, and the rule waits
        # until the backfill is done rather than rank teams on whichever games loaded first.
        boxes = sync_player_games(conn)
        pos_factors = position_factors(conn, season_year)
        stored, total = position_sync_status(conn, season_year)
        known_teams = {r[0] for r in conn.execute("SELECT DISTINCT team_id FROM team_games")}
        conn.close()
        print(f"[main] team defense: {added} new game(s) stored, "
              f"{len({t for t, _ in factors})} teams rated")
        print(f"[main] position defense: {boxes} new box score(s), {stored} of {total} games "
              f"stored{'' if pos_factors else ' — the rule waits for the backfill'}")

    on_slate = {r["player_id"] for r in index.values()
                if method.banned(r["player_id"], r["team_id"])}
    print(f"[method] bans: {len(method.BANNED_PLAYERS)} player(s), "
          f"{len(method.BANNED_TEAMS)} team(s); {len(on_slate)} banned player(s) on this slate")
    # A mistyped team id would ban nobody without a word. Player ids cannot be checked the
    # same way (a player is only seen on the nights he plays), so the count above is it.
    unknown = sorted(t for t in method.BANNED_TEAMS if known_teams and t not in known_teams)
    if unknown:
        print(f"[method] WARNING: banned team id(s) {', '.join(unknown)} match no NBA team — "
              f"check BANNED_TEAMS in parlay/method.py")

    # The method's head-to-head looks back H2H_SEASONS seasons, further than the model's two.
    # Seasons before last are fetched apart from `logs`, so player_form() never sees them.
    older_years = (range(prior_year - 1, season_year - method.H2H_SEASONS, -1)
                   if season_year else range(0))
    older: dict[str, list] = {}

    # Once per run, as in the baseball twin: drop cached files no run will read again (old
    # slate folders, and finished seasons older than any this run reads), so a cache on the
    # volume stays a couple of slates plus the seasons still in use.
    pruned = prune_cache(day, min((prior_year, *older_years)) if season_year else None)
    if pruned:
        print(f"[main] cache: removed {pruned} old slate folder(s) / season log(s)")

    # Injury report. None means it could not be fetched — kept distinct from "nobody is
    # out", so a failed fetch never logs the whole slate as healthy.
    injuries = fetch_injuries()
    team_ids = {g["home_id"] for g in games} | {g["away_id"] for g in games}
    out_minutes: dict[str, dict[str, float]] = {}       # team_id -> {player_id: minutes}
    out_usage: dict[str, dict[str, float]] = {}         # team_id -> {player_id: shots}
    if injuries is None:
        print("[main] injury report unavailable this run — injury columns left NULL")
    elif season_year:
        for pid, inj in injuries.items():
            if inj["status"] == "out" and inj["team_id"] in team_ids:
                if pid not in logs:
                    logs[pid] = pick_season(fetch_gamelog(pid, day, prior_year))
                out_minutes.setdefault(inj["team_id"], {})[pid] = \
                    recent_current_minutes(logs[pid][1], season_year)
                out_usage.setdefault(inj["team_id"], {})[pid] = \
                    recent_current_usage(logs[pid][1], season_year)
        missing = sum(sum(m.values()) for m in out_minutes.values())
        print(f"[main] injuries: {sum(len(m) for m in out_minutes.values())} out on tonight's "
              f"teams, {missing:.0f} min/game of this season's rotation missing")

    candidates: list[dict] = []
    method_legs: list[dict] = []
    unmatched: set[str] = set()
    for market in NBA_MARKETS:
        # One fetch gives the main lines AND the alternate ladder (the owner's undercuts).
        board, ladders = fetch_prop_board(market, book=DEFAULT_BOOK)
        for name, entry in board.items():
            # The board is NOT date-filtered — it carries every slate the provider has up,
            # so on 2026-10-21 it also held 41 players from the 10-20 games. The roster
            # index happened to exclude them, but relying on that made a normal condition
            # look like 43 failed name matches. Filter explicitly and the only names left
            # in `unmatched` are real misses.
            if game_slate_date(entry.get("start")) != day:
                continue
            who = index.get(name)
            if not who:
                unmatched.add(entry.get("player_name") or name)
                continue
            if who["game_id"] not in scheduled:
                in_progress.add(name)
                continue

            pid = who["player_id"]
            if pid not in logs:
                logs[pid] = pick_season(fetch_gamelog(pid, day, prior_year))
            season_label, rows = logs[pid]
            if rows and pid not in older:
                older[pid] = [g for y in older_years for g in fetch_season_log(pid, y)]

            form = player_form(rows, market, opponent_id=who["opponent_id"]) if rows else {}
            stat, team_stat = MARKET_STAT.get(market), MARKET_TEAM_STAT.get(market)
            # The owner's rule inputs at this row's main line — logged for every row.
            own = method.player_inputs(rows, stat, who["opponent_id"]) if rows else {}
            l10 = method.hit_rate(rows, stat, entry["line"], 10)[0] if rows else None
            l20 = method.hit_rate(rows, stat, entry["line"], 20)[0] if rows else None
            sim = (method.similar_opponent_games(rows, stat, who["opponent_id"], factors,
                                                 team_stat) if rows else [])
            meet = (method.meetings(rows + older.get(pid, []), stat, who["opponent_id"])
                    if rows else [])
            pos_factor, pos_rank = pos_factors.get(
                (who["opponent_id"], who.get("position"), team_stat), (None, None))
            prob = prop_probability(form, market, entry.get("line"))
            # Your rules, as a DELTA on top of the quant number — never a replacement.
            # Only applied when the quant model produced a probability: a rule nudging a
            # number that doesn't exist would invent a prediction from nothing.
            adj = 0.0
            if prob is not None:
                adj = apply_prop_owner_logic(entry.get("player_name") or name, market,
                                             who["opponent"], entry.get("line"), prob)
            final = None if prob is None else round(min(max(prob + adj, 0.0), 1.0), 4)

            candidates.append({
                "game_id": who["game_id"], "player_id": pid,
                "player_name": entry.get("player_name") or name,
                "team": who["team"], "team_id": who["team_id"], "opponent": who["opponent"],
                "market": market, "line": entry.get("line"),
                "tipoff": who["tipoff"],
                "season_phase": phase_by_game.get(who["game_id"]),
                # base_prob is the quant number, owner_adj your rules' delta, model_prob
                # the clamped sum. Kept separate so compare_owner_logic() can score the
                # rules against the quant model on identical outcomes. Until 2026-10-26
                # this hardcoded 0.0 and never called the rules at all, so anything
                # written in owner_logic.py would have silently done nothing.
                "base_prob": prob, "owner_adj": adj, "model_prob": final,
                # calibrated_prob stays NULL on purpose. No NBA calibration has been
                # fitted — there is no data to fit one on — and inventing a slope is
                # exactly what made MLB's gate unfireable. The column is here so that
                # when one IS fitted, it cannot be used without being logged.
                "calibrated_prob": None,
                "book_odds": entry.get("over"), "book": entry.get("book"),
                "book_under_odds": entry.get("under"),
                "book_implied": _implied(entry.get("over")),
                "stats_season": season_label, "stat_sd": form.get("stat_sd"),
                "season_avg": form.get("season_avg"),
                "recent_avg": form.get("recent_avg"),
                "recent_games": form.get("recent_games"),
                "minutes_avg": form.get("minutes_avg"),
                "usage_proxy": form.get("usage_proxy"),
                "vs_opp_avg": form.get("vs_opp_avg"),
                "vs_opp_games": form.get("vs_opp_games"),
                "days_rest": days_rest(rows, who["tipoff"]),
                "is_home": who["is_home"],
                # Logged, not used by the model yet — see pipeline/team_defense.
                "opp_allow_factor": factors.get((who["opponent_id"],
                                                 MARKET_TEAM_STAT.get(market))),
                "injury_status": (None if injuries is None else
                                  (injuries.get(pid) or {}).get("status") or "active"),
                "teammate_minutes_out": (None if injuries is None else round(sum(
                    m for p, m in out_minutes.get(who["team_id"], {}).items() if p != pid), 1)),
                "teammate_usage_out": (None if injuries is None else round(sum(
                    u for p, u in out_usage.get(who["team_id"], {}).items() if p != pid), 1)),
                "hit_l10": l10, "hit_l20": l20,
                "sim_opp_avg": method.mean(sim), "sim_opp_games": len(sim),
                "season_avg_raw": own.get("season_avg"),
                # Head-to-head and similar defenses as the method now reads them: games
                # over this line, three seasons back for head-to-head (10-29).
                "h2h_hits": method.over(meet, entry.get("line")) if rows else None,
                "h2h_meetings": len(meet) if rows else None,
                "sim_opp_hits": method.over(sim, entry.get("line")) if rows else None,
                "position": who.get("position"),
                "opp_pos_factor": pos_factor, "opp_pos_rank": pos_rank,
                # Logged on every row, banned or not, so the ban can be measured; the ban
                # filters the method only.
                "banned": method.banned(pid, who["team_id"]) or "no",
            })

            # The owner's method: the main line, plus (points only) every rung 2-5 under it.
            if market in method.METHOD_MARKETS and rows and entry.get("over") is not None:
                rungs = [(entry["line"], entry["over"], 0.0)]
                if market == "player_points":
                    lo, hi = method.POINTS_UNDERCUT
                    ladder = next((l for l in ladders.get(name, [])
                                   if game_slate_date(l["start"]) == day), None)
                    for line, odds in (ladder or {}).get("rungs", []):
                        cut = round(entry["line"] - line, 1)
                        if lo <= cut <= hi:
                            rungs.append((line, odds, cut))
                method_legs.extend(method.legs_for(candidates[-1], rows, stat, rungs, own,
                                                   sim, meet))

    if FETCH_FAILURES["prior_gamelog"]:
        print(f"[main] {FETCH_FAILURES['prior_gamelog']} last-season log(s) unavailable — "
              f"those players fell back to this season alone; retried next run")
    if FETCH_FAILURES["older_gamelog"]:
        print(f"[main] {FETCH_FAILURES['older_gamelog']} older-season log(s) unavailable — "
              f"those players' head-to-head counts two seasons; retried next run")
    if in_progress:
        print(f"[main] {len(in_progress)} priced player(s) skipped: game already underway")
    if unmatched:
        shown = ", ".join(sorted(unmatched)[:6])
        print(f"[main] {len(unmatched)} priced player(s) not matched to a roster: {shown}"
              f"{' ...' if len(unmatched) > 6 else ''}")
    if not candidates:
        print("[main] no priced players matched — nothing logged.")
        return 0

    scored = sum(1 for c in candidates if c["model_prob"] is not None)
    print(f"[main] {len(candidates)} rows, {scored} with a model probability")
    logged = log_predictions(candidates)
    _run_method(day, method_legs)
    return logged


def _run_method(day: str, legs: list[dict]) -> None:
    """Build, store and print the owner's parlays for this slate."""
    from datetime import datetime, timezone

    passing, reasons = method.qualify(legs)
    why = ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
    print(f"[method] {len(legs)} candidate legs, {len(passing)} pass the rules"
          f"{' — failed: ' + why if why else ''}")
    unranked = sum(1 for l in legs if l.get("opp_pos_rank") is None)
    if unranked:
        print(f"[method] {unranked} of {len(legs)} legs have no defense-vs-position rank, "
              f"so that rule could not judge them")
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    parlays, held_out = method.build_slate(passing, method.frozen(conn, day, now))
    for tier, n in held_out.items():
        if n:
            print(f"[method] {tier}: built without {n} player(s) already in another slip")
    for line in method.save(conn, day, parlays, now):
        print(f"[method] {line}")
    print(method.describe(conn, day))
    conn.close()


def _implied(odds: int | None) -> float | None:
    if odds is None:
        return None
    return round(abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100), 4)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "--collect"
    if cmd == "--grade":
        grade_predictions()
    elif cmd == "--collect-grade":
        grade_predictions()
        collect()
    elif cmd == "--report":
        report()
    elif cmd == "--migrate":
        # The web service runs this at startup. It reads this database READ-ONLY, so it
        # cannot apply _connect()'s migrations itself — without this, a column added in a
        # deploy would be missing from the dashboard until the next collect or grade run.
        _connect().close()
    elif cmd == "--parlays":
        # Tonight's method parlays (or a given slate's): python main.py --parlays 2026-10-27
        conn = _connect()
        print(method.describe(conn, sys.argv[2] if len(sys.argv) > 2 else slate_date()))
        conn.close()
    else:
        collect()
