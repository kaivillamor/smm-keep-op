import argparse
from datetime import datetime, timezone

from pipeline.odds_fetcher import fetch_odds, check_usage
from pipeline.stats_fetcher import fetch_stats, fetch_batter_statcast_season
from pipeline.lineup_fetcher import fetch_lineups
from pipeline.weather_fetcher import fetch_weather
from pipeline.prop_pipeline import analyze_hr_props
from pipeline.hit_pipeline import analyze_hit_props
from model.probability_model import build_probabilities
from model.edge_detector import detect_edges
from model.factors.hr_prop_model import RECENT_DAYS
from parlay.leg_selector import select_legs
from parlay.parlay_builder import build_parlay
from llm.context_analyzer import analyze_context
from output.daily_slip import print_slip
from output.backtest import log_parlay, log_hit_parlay, log_hit_parlays, log_hr_candidates, record_hit_payout
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

            parlay = build_parlay(legs)
            print_slip(parlay)
            log_parlay(parlay)

    # ── HR props (65/65/65 gate) ──────────────────────────────────────────────
    if run_props:
        print("\n[main] Running HR prop analysis...")
        from datetime import datetime
        year = str(datetime.now(timezone.utc).year)
        season_batter_stats = fetch_batter_statcast_season(year)
        candidates = analyze_hr_props(lineups, season_batter_stats, stats["probable_pitchers"], stats["pitcher_stats"])
        _print_hr_candidates(candidates)
        log_hr_candidates(candidates)

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
        log_hit_parlays(leg_ids)


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
            f"          Model Prob: {leg['hit_probability'] * 100:.1f}%{note_str}"
        )

    combined = 1.0
    for leg in legs:
        combined *= leg["hit_probability"]
    print(f"\n  Combined (if indep.): {combined * 100:.1f}%")
    print(f"  Note: Book odds not shown — requires player props API tier.")
    print(f"        Cross-check lines at DraftKings / FanDuel before placing.")
    print(f"{'=' * width}")


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
    stake = 50
    print(f"\n{'=' * width}")
    print(f"  HIT PARLAY SPLIT — {len(pairs)} × 2-leg  |  ${stake} each  |  ${stake * len(pairs)} total")
    print(f"{'=' * width}")

    if not pairs:
        print("  No pairs generated (no confirmed lineups?).")
        print(f"{'=' * width}")
        return

    for p_idx, pair in enumerate(pairs, 1):
        combined = 1.0
        for leg in pair:
            combined *= leg["hit_probability"]
        print(f"\n  PARLAY {p_idx}  —  combined: {combined * 100:.1f}%  |  stake: ${stake}")
        print(f"  {'─' * (width - 2)}")
        for leg in pair:
            hand_label = "RHP" if leg["pitcher_hand"] == "R" else "LHP"
            print(
                f"  {leg['batter_name']} ({leg['team']}) 1+ Hit "
                f"vs {leg['pitcher_name']} ({hand_label})  "
                f"{leg['hit_probability'] * 100:.1f}%"
            )

    print(f"\n  Note: Book odds required to place — check FanDuel/DK for 1+ hit lines.")
    print(f"{'=' * width}")


def _print_hr_candidates(candidates: list[dict]) -> None:
    if not candidates:
        print("[main] No HR prop candidates passed the gate today.")
        return

    print(f"\n{'=' * 50}")
    print(f"  HR PROP CANDIDATES — {len(candidates)} passed 65/65/65 gate")
    print(f"{'=' * 50}")
    for c in candidates:
        s = c["scores"]
        hrfb_str = f"{s['pitcher_hr_fb']}%" if s.get("pitcher_hr_fb") is not None else "N/A"
        print(
            f"  {c['batter_name']} ({c['team']})\n"
            f"    Barrel Rate:        {s['barrel_rate']}%\n"
            f"    Sweet Spot:         {s['sweet_spot']}%\n"
            f"    Hard Contact (L{RECENT_DAYS}d): {s['recent_hard_contact']}%\n"
            f"    Zone Fit:           {s['zone_fit']}\n"
            f"    Pitcher HR/FB:      {hrfb_str}\n"
        )
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
    args = parser.parse_args()

    if args.usage:
        _print_usage()
    elif args.results:
        resolve_pending()
    elif args.record_hit_win:
        date_str, parlay_num, payout = args.record_hit_win
        record_hit_payout(date_str, int(parlay_num), float(payout))
    else:
        run(use_llm=not args.no_llm, run_props=args.props, run_hits=args.hits,
            run_hits_2=args.hits_2, run_all=args.all)
