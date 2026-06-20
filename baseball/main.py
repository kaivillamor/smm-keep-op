import argparse
from datetime import datetime, timezone

from pipeline.odds_fetcher import fetch_odds
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
from output.backtest import log_parlay


def run(use_llm: bool = True, run_props: bool = False, run_hits: bool = False):
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
        candidates = analyze_hr_props(lineups, season_batter_stats, stats["probable_pitchers"])
        _print_hr_candidates(candidates)

    # ── Hit parlay (1+ hit, top-6 lineup spots) ───────────────────────────────
    if run_hits:
        print("\n[main] Running hit parlay analysis...")
        hit_legs = analyze_hit_props(lineups, stats)
        _print_hit_parlay(hit_legs)


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
        owner_adj = leg.get("owner_adj", 0.0)
        adj_str = f"  [owner +{owner_adj*100:.1f}%]" if owner_adj > 0 else (
                  f"  [owner {owner_adj*100:.1f}%]" if owner_adj < 0 else "")
        print(
            f"  LEG {i}: {leg['batter_name']} ({leg['team']})\n"
            f"          1+ Hit vs {leg['pitcher_name']} ({hand_label})\n"
            f"          Model Prob: {leg['hit_probability'] * 100:.1f}%{adj_str}"
        )

    combined = 1.0
    for leg in legs:
        combined *= leg["hit_probability"]
    print(f"\n  Combined (if indep.): {combined * 100:.1f}%")
    print(f"  Note: Book odds not shown — requires player props API tier.")
    print(f"        Cross-check lines at DraftKings / FanDuel before placing.")
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
        print(
            f"  {c['batter_name']} ({c['team']})\n"
            f"    Barrel Rate:        {s['barrel_rate']}%\n"
            f"    Sweet Spot:         {s['sweet_spot']}%\n"
            f"    Hard Contact (L{RECENT_DAYS}d): {s['recent_hard_contact']}%\n"
            f"    Zone Fit:           {s['zone_fit']}\n"
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
        "--hits",
        action="store_true",
        help="Generate a hit parlay (1+ hit per leg) from top-6 lineup spots in confirmed games",
    )
    args = parser.parse_args()
    run(use_llm=not args.no_llm, run_props=args.props, run_hits=args.hits)
