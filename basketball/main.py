"""
NBA prop research collector.

    python main.py --collect        score tonight's priced players and log them
    python main.py --grade          grade finished games from box scores
    python main.py --collect-grade  grade yesterday, then collect tonight
    python main.py --report         calibration + ranking signal

Research only. Nothing here places or records a bet, and there is deliberately no betting
gate: MLB shipped one before its inputs were logged and it could not fire for three weeks
without anyone noticing (PROJECT.md §29). Collect and measure first.
"""
import sys

from model.prop_model import prop_probability
from output.research import grade_predictions, log_predictions, report
from pipeline.calendar import game_slate_date, season_phase, slate_date
from pipeline.prop_odds import DEFAULT_BOOK, NBA_MARKETS, fetch_prop_odds, normalize_name
from pipeline.stats_fetcher import (build_player_index, days_rest, fetch_gamelog,
                                    fetch_scoreboard, pick_season, player_form)


def collect(day: str | None = None) -> int:
    """Score every player with a posted line on tonight's slate, and log them all.

    EVERY priced player, not just the ones the model likes. Logging only the surfaced
    picks is a biased sample — the ones the model already rated highly — and it cannot
    answer whether the ranking works at all. That realisation is what made MLB's
    calibration measurable in days instead of months.
    """
    day = day or slate_date()
    phase = season_phase(day)
    print(f"[main] slate {day} ({phase or 'unknown phase'}) — research only, no bets")

    games = [g for g in fetch_scoreboard(day) if not g["final"]]
    if not games:
        print("[main] no scheduled games on this slate.")
        return 0
    print(f"[main] {len(games)} game(s)")

    index = build_player_index(games, day, normalize_name)
    print(f"[main] {len(index)} players on rosters")

    # One game log per PLAYER, reused across all four markets. Fetching per player-market
    # would quadruple the calls for identical data.
    logs: dict[str, tuple] = {}

    candidates: list[dict] = []
    unmatched: set[str] = set()
    for market in NBA_MARKETS:
        board = fetch_prop_odds(market, book=DEFAULT_BOOK)
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

            pid = who["player_id"]
            if pid not in logs:
                logs[pid] = pick_season(fetch_gamelog(pid, day))
            season_label, rows = logs[pid]

            form = player_form(rows, market) if rows else {}
            prob = prop_probability(form, market, entry.get("line"))

            candidates.append({
                "game_id": who["game_id"], "player_id": pid,
                "player_name": entry.get("player_name") or name,
                "team": who["team"], "opponent": who["opponent"],
                "market": market, "line": entry.get("line"),
                "tipoff": who["tipoff"],
                # base_prob is the quant number and owner_adj the personal-logic delta,
                # kept separate so either can be measured alone. There is no personal
                # logic yet, hence 0.0 — and it is LOGGED as 0.0 rather than omitted.
                "base_prob": prob, "owner_adj": 0.0, "model_prob": prob,
                # calibrated_prob stays NULL on purpose. No NBA calibration has been
                # fitted — there is no data to fit one on — and inventing a slope is
                # exactly what made MLB's gate unfireable. The column is here so that
                # when one IS fitted, it cannot be used without being logged.
                "calibrated_prob": None,
                "book_odds": entry.get("over"), "book": entry.get("book"),
                "book_implied": _implied(entry.get("over")),
                "stats_season": season_label, "stat_sd": form.get("stat_sd"),
                "season_avg": form.get("season_avg"),
                "recent_avg": form.get("recent_avg"),
                "recent_games": form.get("recent_games"),
                "minutes_avg": form.get("minutes_avg"),
                "usage_proxy": form.get("usage_proxy"),
                "vs_opp_avg": None, "vs_opp_games": None,   # not built yet; logged NULL
                "days_rest": days_rest(rows, who["tipoff"]),
                "is_home": who["is_home"],
                "opp_def_rating": None,                      # logged first, used later
            })

    if unmatched:
        shown = ", ".join(sorted(unmatched)[:6])
        print(f"[main] {len(unmatched)} priced player(s) not matched to a roster: {shown}"
              f"{' ...' if len(unmatched) > 6 else ''}")
    if not candidates:
        print("[main] no priced players matched — nothing logged.")
        return 0

    scored = sum(1 for c in candidates if c["model_prob"] is not None)
    print(f"[main] {len(candidates)} rows, {scored} with a model probability")
    return log_predictions(candidates)


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
    else:
        collect()
