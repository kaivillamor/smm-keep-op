import argparse
from datetime import date, datetime, timezone

from pipeline.odds_fetcher import fetch_odds, check_usage
from pipeline.stats_fetcher import fetch_stats, fetch_batter_statcast_season
from pipeline.lineup_fetcher import fetch_lineups
from pipeline.weather_fetcher import fetch_weather
from pipeline.prop_pipeline import analyze_hr_props
from pipeline.hit_pipeline import analyze_hit_props
from model.probability_model import build_probabilities
from model.edge_detector import detect_edges
from model.factors.hr_prop_model import RECENT_DAYS, HR_PARLAY_STAKE
from model.factors.hit_model import HIT_BASE_STAKE, HIT_PROFIT_FLOOR, HIT_MAX_STAKE
from parlay.leg_selector import select_legs
from parlay.parlay_builder import build_parlay, stake_to_win, profit_for_stake, combine_odds, recommended_stake
from llm.context_analyzer import analyze_context
from output.daily_slip import print_slip
from output.backtest import log_parlays, log_hit_parlay, log_hit_parlays, log_hr_candidates, log_hr_parlays, record_hit_payout, record_hit_odds
from output.result_tracker import resolve_pending


def run(use_llm: bool = True, run_props: bool = False, run_hits: bool = False,
        run_hits_2: bool = False, run_all: bool = False):
    if run_all:
        run_props = True
        run_hits  = True
    print("\n[main] Fetching data...")
    odds    = fetch_odds()
    stats   = fetch_stats()
    lineups = fetch_lineups()
    weather = fetch_weather(odds)

    if not odds:
        print("[main] No games found today.")
        return

    # ── game lines (moneylines + totals) ─────────────────────────────────────
    print(f"\n[main] Building probabilities for {len(odds)} games...")
    probabilities = build_probabilities(odds, stats, lineups, weather)

    print("[main] Detecting edges...")
    edges = detect_edges(probabilities)

    if not edges:
        print("[main] No edges found today — no parlay generated.")
    else:
        print("[main] Selecting legs...")
        legs = select_legs(edges)

        if not legs:
            print("[main] No legs passed filters today.")
        else:
            if use_llm:
                print("[main] Running LLM context check...")
                legs = analyze_context(legs)
            else:
                print("[main] Skipping LLM layer.")

            game_pairs = _pair_game_legs(legs)
            parlays = [build_parlay(pair) for pair in game_pairs]
            for i, parlay in enumerate(parlays, 1):
                print_slip(parlay, num=i if len(parlays) > 1 else None)
            log_parlays(parlays)

    # ── HR props (65/65/65 gate) ──────────────────────────────────────────────
    if run_props:
        print("\n[main] Running HR prop analysis...")
        from datetime import datetime
        year = str(datetime.now(timezone.utc).year)
        season_batter_stats = fetch_batter_statcast_season(year)
        candidates = analyze_hr_props(lineups, season_batter_stats, stats["probable_pitchers"], stats["pitcher_stats"])
        _print_hr_candidates(candidates)
        cand_ids = log_hr_candidates(candidates)
        hr_pairs = _pair_hit_legs(candidates)   # same interleaved pairing as hit parlays
        _print_hr_parlays(hr_pairs)
        log_hr_parlays(cand_ids, stake=HR_PARLAY_STAKE)

    # ── Hit parlay (1+ hit, top-6 lineup spots) ───────────────────────────────
    if run_hits:
        print("\n[main] Running hit parlay analysis (8-leg)...")
        hit_legs = analyze_hit_props(lineups, stats)
        _print_hit_parlay(hit_legs)
        log_hit_parlay(hit_legs)

    if run_hits_2:
        print("\n[main] Running hit parlay analysis (4×2-leg split)...")
        hit_legs = analyze_hit_props(lineups, stats)
        pairs = _pair_hit_legs(hit_legs)
        _print_hit_parlay_split(pairs)
        leg_ids = log_hit_parlay(hit_legs)
        log_hit_parlays(leg_ids, base_stake=HIT_BASE_STAKE,
                        profit_floor=HIT_PROFIT_FLOOR, max_stake=HIT_MAX_STAKE)


def _collect_predictions() -> None:
    """Research-only run: score every batter on the slate and record the predictions.

    Deliberately does NOT call any log_* function from backtest.py — no legs, no
    parlays, no stakes. Simulated results stay in research.db and can never appear
    in the P&L.

    Run it EARLY in the day: the pipeline only scores games that haven't started,
    so a late run captures just the remaining slate (~60 batters) instead of the
    full one (~180). Book odds are recorded for the surfaced legs only, since that
    is where the pipeline attaches them; the calibration question needs just
    model_prob and outcome, both of which are captured for every batter.
    """
    from output.research import log_predictions, report
    print("\n[main] Research collection — no bets logged, bets.db untouched.")
    stats   = fetch_stats()
    lineups = fetch_lineups()

    all_candidates: list[dict] = []
    analyze_hit_props(lineups, stats, all_candidates_out=all_candidates)
    log_predictions(all_candidates)
    report()


def _already_collected(slate: str) -> set[str]:
    """game_pks already recorded for this slate — makes the watcher resumable."""
    from output.research import _connect
    conn = _connect()
    rows = conn.execute(
        "SELECT DISTINCT game_pk FROM predictions WHERE date=?", (slate,)
    ).fetchall()
    conn.close()
    return {str(r["game_pk"]) for r in rows}


def _watch_lineups(interval_min: int = 10, max_hours: float = 14.0) -> None:
    """Poll for lineup confirmations and collect each game the moment it's ready.

    Lineups post in waves 1–3h before first pitch, so a single daily run always
    misses part of the slate. This watches instead: every cycle it scores any game
    that is newly confirmed and hasn't started, then sleeps. Already-collected games
    are skipped (state lives in research.db, so restarting resumes cleanly), and it
    exits once every game is either collected or underway.

    Research only — logs predictions, never bets.
    """
    import time
    from output.research import log_predictions
    from pipeline.stats_fetcher import game_has_started, slate_date
    from pipeline.hit_pipeline import _match_probable

    slate     = slate_date()
    collected = _already_collected(slate)
    started   = time.time()
    print(f"\n[watch] slate {slate} | polling every {interval_min} min | Ctrl-C to stop")
    if collected:
        print(f"[watch] resuming — {len(collected)} game(s) already collected")

    stats = fetch_stats()
    try:
        while True:
            if slate_date() != slate:
                print("[watch] slate rolled over to a new day — stopping.")
                break
            if time.time() - started > max_hours * 3600:
                print(f"[watch] hit the {max_hours}h limit — stopping.")
                break

            lineups = fetch_lineups()
            ready = {
                pk: v for pk, v in lineups.items()
                if v.get("confirmed")
                and not game_has_started(v.get("commence_time"))
                and str(pk) not in collected
            }

            if ready:
                names = ", ".join(f"{v.get('away_team')} @ {v.get('home_team')}"
                                  for v in list(ready.values())[:3])
                more  = f" (+{len(ready) - 3} more)" if len(ready) > 3 else ""
                print(f"[watch] {len(ready)} newly confirmed: {names}{more}")
                # A game whose probable pitcher appeared after startup needs fresh stats.
                # Test for None, not falsiness — an empty-but-present entry is a real
                # match, and treating it as missing triggers a needless 30+ call refetch.
                if any(_match_probable(pk, stats.get("probable_pitchers", {})) is None
                       for pk in ready):
                    print("[watch] refreshing probable pitchers...")
                    stats = fetch_stats()
                bucket: list[dict] = []
                analyze_hit_props(ready, stats, all_candidates_out=bucket)
                if bucket:
                    log_predictions(bucket)
                collected |= {str(pk) for pk in ready}

            waiting = [
                v for pk, v in lineups.items()
                if not game_has_started(v.get("commence_time")) and str(pk) not in collected
            ]
            if not waiting:
                print("[watch] every game collected or underway — done for today.")
                break

            unconfirmed = sum(1 for v in waiting if not v.get("confirmed"))
            print(f"[watch] {len(waiting)} game(s) still upcoming "
                  f"({unconfirmed} awaiting lineups) — next check in {interval_min} min")
            time.sleep(interval_min * 60)
    except KeyboardInterrupt:
        print("\n[watch] stopped by user.")

    print(f"[watch] collected {len(collected)} game(s) for {slate}. "
          f"Run 'python main.py --collect-grade' tomorrow morning.")


def _print_usage() -> None:
    usage = check_usage()
    if not usage:
        return

    used      = usage["used"]
    remaining = usage["remaining"]
    total     = usage["total"]
    bar_filled = round((used / total) * 20) if total else 0
    bar = "█" * bar_filled + "░" * (20 - bar_filled)

    print(f"\n{'=' * 40}")
    print(f"  THE ODDS API — MONTHLY USAGE")
    print(f"{'=' * 40}")
    print(f"  [{bar}]")
    print(f"  Used:      {used:>5} / {total}")
    print(f"  Remaining: {remaining:>5} / {total}")
    print(f"  Reset:     {usage['reset_date']}  ({usage['days_until_reset']} days)")
    print(f"{'=' * 40}\n")


def _fmt_leg_odds(leg: dict) -> str:
    """Formats a leg's book odds + EV edge for display, or a fallback if unposted."""
    odds = leg.get("book_odds")
    if odds is None:
        return "book: no line posted"
    odds_str = f"+{odds}" if odds > 0 else str(odds)
    book     = (leg.get("book") or "").title()
    ev       = leg.get("ev")
    ev_str   = f"  EV {ev * 100:+.1f}%" if ev is not None else ""
    return f"{odds_str} @ {book}{ev_str}"


def _print_hit_parlay(legs: list[dict]) -> None:
    width = 54
    print(f"\n{'=' * width}")
    print(f"  TODAY'S HIT PARLAY  —  {len(legs)}-leg / 1+ Hit each")
    print(f"{'=' * width}")

    if not legs:
        print("  No hit parlay legs generated (no confirmed lineups?).")
        print(f"{'=' * width}")
        return

    for i, leg in enumerate(legs, 1):
        hand_label = "RHP" if leg["pitcher_hand"] == "R" else "LHP"
        owner_adj  = leg.get("owner_adj", 0.0)

        notes = []
        h2h_ab  = leg.get("h2h_ab", 0)
        h2h_avg = leg.get("h2h_avg")
        if h2h_ab >= 10 and h2h_avg is not None:
            notes.append(f"H2H: {h2h_avg:.3f} ({h2h_ab} AB)")

        recent_ab  = leg.get("recent_ab", 0)
        recent_avg = leg.get("recent_avg")
        if recent_ab >= 10 and recent_avg is not None:
            notes.append(f"L14: {recent_avg:.3f} ({recent_ab} AB)")

        venue_ab  = leg.get("venue_ab", 0)
        venue_avg = leg.get("venue_avg")
        if venue_ab >= 20 and venue_avg is not None:
            notes.append(f"at venue: {venue_avg:.3f} ({venue_ab} AB)")

        pitcher_h9  = leg.get("pitcher_recent_h9")
        days_rest   = leg.get("pitcher_days_rest")
        if pitcher_h9 is not None:
            rest_str = f", {days_rest}d rest" if days_rest is not None else ""
            notes.append(f"P recent H/9: {pitcher_h9:.1f}{rest_str}")

        team_avg = leg.get("team_recent_avg")
        if team_avg is not None:
            trend = "hot" if team_avg >= 0.265 else ("cold" if team_avg <= 0.225 else "avg")
            notes.append(f"team L14: {team_avg:.3f} ({trend})")

        if leg.get("is_day_game"):
            notes.append("day game")

        if owner_adj != 0.0:
            sign = "+" if owner_adj > 0 else ""
            notes.append(f"owner {sign}{owner_adj*100:.1f}%")

        note_str = f"  [{', '.join(notes)}]" if notes else ""
        print(
            f"  LEG {i}: {leg['batter_name']} ({leg['team']})\n"
            f"          1+ Hit vs {leg['pitcher_name']} ({hand_label})\n"
            f"          Model Prob: {leg['hit_probability'] * 100:.1f}%   {_fmt_leg_odds(leg)}{note_str}"
        )

    combined = 1.0
    for leg in legs:
        combined *= leg["hit_probability"]
    print(f"\n  Combined (if indep.): {combined * 100:.1f}%")
    book_combined = combine_odds([leg.get("book_odds") for leg in legs])
    if book_combined is not None:
        odds_str = f"+{book_combined}" if book_combined > 0 else str(book_combined)
        print(f"  Book parlay odds:     {odds_str}  (FanDuel)")
    else:
        print(f"  Book parlay odds:     — (one or more legs had no posted line)")
    print(f"{'=' * width}")


def _print_stake_calc(combined_odds: int, target_win: float) -> None:
    odds_str = f"+{combined_odds}" if combined_odds > 0 else str(combined_odds)
    stake    = stake_to_win(combined_odds, target_win)
    total    = round(stake + target_win, 2)
    print(f"\n{'=' * 40}")
    print(f"  STAKE-TO-WIN CALCULATOR")
    print(f"{'=' * 40}")
    print(f"  Combined odds:   {odds_str}")
    print(f"  Target winnings: ${target_win:.2f}")
    print(f"{'─' * 40}")
    print(f"  → Stake:         ${stake:.2f}")
    print(f"  → Total payout:  ${total:.2f}  (stake + winnings)")
    print(f"{'=' * 40}\n")


def _pair_game_legs(legs: list[dict]) -> list[list[dict]]:
    """
    Splits game legs (sorted by edge desc) into 2-leg parlays instead of one
    4-leg parlay — legs at ~55% only win a 4-leg ~9% of the time, which buries
    the edge under variance. Interleaved (1&3, 2&4) so pairs are balanced.
    A lone qualifying leg is bet straight; an odd lowest-edge leg is dropped.
    """
    if len(legs) == 1:
        return [legs]
    if len(legs) % 2:
        legs = legs[:-1]
    half = len(legs) // 2
    return [[legs[i], legs[i + half]] for i in range(half)]


def _pair_hit_legs(legs: list[dict]) -> list[list[dict]]:
    """
    Splits hit parlay legs into 4 interleaved pairs for the 2-leg split mode.
    Interleaved pairing (1&5, 2&6, 3&7, 4&8) ensures each pair has one
    higher-probability and one moderate leg rather than concentrating the
    best legs in a single pair.
    """
    half = len(legs) // 2
    pairs = []
    for i in range(half):
        pair = [legs[i], legs[i + half]]
        pairs.append(pair)
    return pairs


def _print_hit_parlay_split(pairs: list[list[dict]]) -> None:
    width = 54
    print(f"\n{'=' * width}")
    print(f"  HIT PARLAY SPLIT — {len(pairs)} × 2-leg  |  double-up / ${HIT_PROFIT_FLOOR:.0f} floor")
    print(f"{'=' * width}")

    if not pairs:
        # Don't guess at the cause — the pipeline prints the real reason above
        # (unconfirmed lineups, games already started, or the probability gate).
        print("  No pairs generated — see the [hit_pipeline] lines above for why.")
        print(f"{'=' * width}")
        return

    for p_idx, pair in enumerate(pairs, 1):
        combined = 1.0
        for leg in pair:
            combined *= leg["hit_probability"]
        book_combined = combine_odds([leg.get("book_odds") for leg in pair])
        if book_combined is not None:
            odds_str  = f"+{book_combined}" if book_combined > 0 else str(book_combined)
            stake     = recommended_stake(book_combined, HIT_BASE_STAKE,
                                          HIT_PROFIT_FLOOR, HIT_MAX_STAKE)
            to_win    = round(profit_for_stake(book_combined, stake), 2)
            capped    = "  [capped @ $%.0f]" % HIT_MAX_STAKE if stake >= HIT_MAX_STAKE else ""
            price_str = f"book {odds_str} → stake ${stake:.2f} to win ${to_win:.2f}{capped}"
        else:
            price_str = "book: incomplete lines (stake base once priced)"
        print(f"\n  PARLAY {p_idx}  —  model: {combined * 100:.1f}%  |  {price_str}")
        print(f"  {'─' * (width - 2)}")
        for leg in pair:
            hand_label = "RHP" if leg["pitcher_hand"] == "R" else "LHP"
            print(
                f"  {leg['batter_name']} ({leg['team']}) 1+ Hit "
                f"vs {leg['pitcher_name']} ({hand_label})  "
                f"{leg['hit_probability'] * 100:.1f}%   {_fmt_leg_odds(leg)}"
            )

    print(f"\n  Odds auto-recorded from the prop-odds API (FanDuel, 60s delayed).")
    print(f"  If your slip differs, override: python main.py --record-hit-placed <date> <#> <odds>")
    print(f"{'=' * width}")


def _print_hr_parlays(pairs: list[list[dict]]) -> None:
    width = 54
    print(f"\n{'=' * width}")
    print(f"  HR PARLAY — {len(pairs)} × 2-leg  |  ${HR_PARLAY_STAKE:.0f} each  |  both to hit 1+ HR")
    print(f"{'=' * width}")

    if not pairs:
        print("  No HR pairs generated — see the [prop_pipeline] lines above for why.")
        print(f"{'=' * width}")
        return

    for p_idx, pair in enumerate(pairs, 1):
        combined = combine_odds([c.get("book_odds") for c in pair])
        if combined is not None:
            odds_str  = f"+{combined}" if combined > 0 else str(combined)
            to_win    = round(profit_for_stake(combined, HR_PARLAY_STAKE), 2)
            price_str = f"book {odds_str} → win ${to_win:.2f} on ${HR_PARLAY_STAKE:.0f}"
        else:
            price_str = "book: no HR line for one+ leg"
        print(f"\n  PARLAY {p_idx}  —  {price_str}")
        print(f"  {'─' * (width - 2)}")
        for c in pair:
            o = c.get("book_odds")
            leg_str = (f"{'+' if o > 0 else ''}{o}") if o is not None else "no line posted"
            print(f"  {c['batter_name']} ({c['team']}) 1+ HR   {leg_str}")

    print(f"\n  Odds auto-recorded from the prop-odds API (FanDuel, 60s delayed).")
    print(f"{'=' * width}")


def _print_hr_candidates(candidates: list[dict]) -> None:
    if not candidates:
        print("[main] No HR prop candidates passed the gate today.")
        return

    print(f"\n{'=' * 50}")
    print(f"  HR PROP CANDIDATES — top {len(candidates)} by rank score")
    print(f"{'=' * 50}")
    for i, c in enumerate(candidates, 1):
        s = c["scores"]
        hrfb_str = f"{s['pitcher_hr_fb']}%*" if s.get("pitcher_hr_fb") is not None else "N/A"
        gate_str = s.get("gate_triggered") or "?"
        rank     = c.get("rank_score", 0.0)
        print(
            f"  #{i} {c['batter_name']} ({c['team']})  [score: {rank}  gate: {gate_str}]\n"
            f"    Barrel Rate:        {s['barrel_rate']}%\n"
            f"    Sweet Spot:         {s['sweet_spot']}%\n"
            f"    Hard Contact (L{RECENT_DAYS}d): {s['recent_hard_contact']}%\n"
            f"    Zone Fit:           {s['zone_fit']}\n"
            f"    Pitcher HR/FB:      {hrfb_str}\n"
        )
    print("  * Pitcher HR/FB estimated from FIP-xFIP gap")
    print("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MLB value betting pipeline")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the LLM context check and run quant model only",
    )
    parser.add_argument(
        "--props",
        action="store_true",
        help="Run HR prop analysis using the 65/65/65 gate (Sweet Spot / Hard Contact / Zone Fit)",
    )
    parser.add_argument(
        "--hits", "--hits-8",
        dest="hits",
        action="store_true",
        help="8-leg hit parlay list at $10 (default hit mode)",
    )
    parser.add_argument(
        "--hits-2",
        dest="hits_2",
        action="store_true",
        help="4×2-leg hit parlays at $50 each (interleaved pairing)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run everything: game lines + HR props + hit parlay in one command",
    )
    parser.add_argument(
        "--usage",
        action="store_true",
        help="Show Odds API credit usage and monthly reset date, then exit",
    )
    parser.add_argument(
        "--results",
        action="store_true",
        help="Grade yesterday's pending parlay and hit legs against actual game results",
    )
    parser.add_argument(
        "--record-hit-win",
        nargs=3,
        metavar=("DATE", "PARLAY_NUM", "PAYOUT"),
        help="Record actual payout for a winning hit parlay, e.g. --record-hit-win 2026-06-22 1 127.50",
    )
    parser.add_argument(
        "--record-hit-placed",
        nargs=3,
        metavar=("DATE", "PARLAY_NUM", "ODDS"),
        help="Record combined odds from the bet slip when placing a hit parlay, "
             "e.g. --record-hit-placed 2026-07-06 1 +120 — payout then auto-computes on a win",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Research mode: score every batter and log predictions to the SEPARATE "
             "research DB. Places/logs no bets and never touches bets.db or the P&L.",
    )
    parser.add_argument(
        "--collect-grade",
        action="store_true",
        help="Grade pending research predictions against box scores (research DB only)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run --collect continuously: poll for lineup confirmations and collect each "
             "game as it becomes ready, until the whole slate is collected or underway",
    )
    parser.add_argument(
        "--watch-interval",
        type=int,
        default=10,
        metavar="MIN",
        help="Minutes between --watch polls (default 10)",
    )
    parser.add_argument(
        "--stake-calc",
        nargs="+",
        metavar="ODDS [TARGET_WIN]",
        help="Given combined American odds, show the stake needed to win a target "
             "profit (default $50). E.g. --stake-calc +198  or  --stake-calc -120 30",
    )
    args = parser.parse_args()

    if args.usage:
        _print_usage()
    elif args.watch:
        _watch_lineups(interval_min=args.watch_interval)
    elif args.collect:
        _collect_predictions()
    elif args.collect_grade:
        from output.research import grade_predictions, report
        grade_predictions()
        report()
    elif args.results:
        resolve_pending()
    elif args.record_hit_win:
        date_str, parlay_num, payout = args.record_hit_win
        record_hit_payout(date_str, int(parlay_num), float(payout))
    elif args.record_hit_placed:
        date_str, parlay_num, odds = args.record_hit_placed
        record_hit_odds(date_str, int(parlay_num), int(odds))
    elif args.stake_calc:
        odds       = int(args.stake_calc[0].lstrip("+"))
        target_win = float(args.stake_calc[1]) if len(args.stake_calc) > 1 else 50.0
        _print_stake_calc(odds, target_win)
    else:
        run(use_llm=not args.no_llm, run_props=args.props, run_hits=args.hits,
            run_hits_2=args.hits_2, run_all=args.all)
