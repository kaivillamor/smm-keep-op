from pipeline.odds_fetcher import fetch_odds
from pipeline.stats_fetcher import fetch_stats
from pipeline.lineup_fetcher import fetch_lineups
from pipeline.weather_fetcher import fetch_weather
from model.probability_model import build_probabilities
from model.edge_detector import detect_edges
from parlay.leg_selector import select_legs
from parlay.parlay_builder import build_parlay
from llm.context_analyzer import analyze_context
from output.daily_slip import print_slip
from output.backtest import log_parlay


def run():
    print("\n[main] Fetching data...")
    odds     = fetch_odds()
    stats    = fetch_stats()
    lineups  = fetch_lineups()
    weather  = fetch_weather(odds)   # needs game list to match game times to forecasts

    if not odds:
        print("[main] No games found today.")
        return

    print(f"\n[main] Building probabilities for {len(odds)} games...")
    probabilities = build_probabilities(odds, stats, lineups, weather)

    print("[main] Detecting edges...")
    edges = detect_edges(probabilities)

    if not edges:
        print("[main] No edges found today — no parlay generated.")
        return

    print("[main] Selecting legs...")
    legs = select_legs(edges)

    if not legs:
        print("[main] No legs passed filters today.")
        return

    print("[main] Running LLM context check...")
    legs = analyze_context(legs)

    parlay = build_parlay(legs)
    print_slip(parlay)
    log_parlay(parlay)


if __name__ == "__main__":
    run()
