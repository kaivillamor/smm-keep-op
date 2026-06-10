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


def run():
    odds = fetch_odds()
    stats = fetch_stats()
    lineups = fetch_lineups()
    weather = fetch_weather()

    probabilities = build_probabilities(odds, stats, lineups, weather)
    edges = detect_edges(probabilities)

    legs = select_legs(edges)
    legs = analyze_context(legs)
    parlay = build_parlay(legs)

    print_slip(parlay)


if __name__ == "__main__":
    run()
