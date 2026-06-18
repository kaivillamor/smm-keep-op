import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"
BOOKMAKERS = ["draftkings", "fanduel", "betmgm", "williamhill_us"]
MARKETS = ["h2h", "totals"]


def fetch_odds() -> list[dict]:
    raw = _fetch_raw()
    normalized = [_normalize_game(g) for g in raw]
    _save_raw(raw)
    return normalized


def _fetch_raw() -> list[dict]:
    url = f"{BASE_URL}/sports/baseball_mlb/odds"
    params = {
        "regions": "us",
        "markets": ",".join(MARKETS),
        "bookmakers": ",".join(BOOKMAKERS),
        "oddsFormat": "american",
        "apiKey": ODDS_API_KEY,
    }
    response = requests.get(url, params=params)
    response.raise_for_status()

    remaining = response.headers.get("x-requests-remaining", "?")
    used = response.headers.get("x-requests-used", "?")
    print(f"[odds_fetcher] API credits used: {used} | remaining: {remaining}")

    return response.json()


def _normalize_game(game: dict) -> dict:
    bookmakers = {}
    for bm in game.get("bookmakers", []):
        key = bm["key"]
        bookmakers[key] = {}
        for market in bm.get("markets", []):
            if market["key"] == "h2h":
                outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
                bookmakers[key]["h2h"] = {
                    "home": outcomes.get(game["home_team"]),
                    "away": outcomes.get(game["away_team"]),
                }
            elif market["key"] == "totals":
                over = next((o for o in market["outcomes"] if o["name"] == "Over"), None)
                under = next((o for o in market["outcomes"] if o["name"] == "Under"), None)
                bookmakers[key]["totals"] = {
                    "line": over["point"] if over else None,
                    "over": over["price"] if over else None,
                    "under": under["price"] if under else None,
                }

    return {
        "id": game["id"],
        "commence_time": game["commence_time"],
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "bookmakers": bookmakers,
        "best_lines": _find_best_lines(game["home_team"], game["away_team"], bookmakers),
    }


def _find_best_lines(home: str, away: str, bookmakers: dict) -> dict:
    best = {"home_ml": None, "away_ml": None, "over": None, "under": None}

    for book, markets in bookmakers.items():
        h2h = markets.get("h2h", {})
        totals = markets.get("totals", {})

        if h2h.get("home") is not None:
            if best["home_ml"] is None or h2h["home"] > best["home_ml"]["odds"]:
                best["home_ml"] = {"odds": h2h["home"], "book": book}
        if h2h.get("away") is not None:
            if best["away_ml"] is None or h2h["away"] > best["away_ml"]["odds"]:
                best["away_ml"] = {"odds": h2h["away"], "book": book}
        if totals.get("over") is not None:
            if best["over"] is None or totals["over"] > best["over"]["odds"]:
                best["over"] = {"odds": totals["over"], "line": totals["line"], "book": book}
        if totals.get("under") is not None:
            if best["under"] is None or totals["under"] > best["under"]["odds"]:
                best["under"] = {"odds": totals["under"], "line": totals["line"], "book": book}

    return best


def fetch_hr_prop_odds() -> list[dict]:
    """
    Fetches HR prop odds for today's games from The Odds API.

    Requires The Odds API player props tier ($50+/month as of 2025).
    Uses per-event endpoint: one call per game to get batter_home_runs market.
    Returns a flat list of {batter_name, game_id, home_team, away_team, book, odds, point}.

    NOTE: odds are "to score 1+ HRs" (point = 0.5 threshold, Over only).
    """
    if not ODDS_API_KEY:
        print("[odds_fetcher] No ODDS_API_KEY — skipping HR prop odds")
        return []

    today = datetime.utcnow().date().isoformat()
    events = _fetch_today_events(today)
    if not events:
        return []

    all_props = []
    for event in events:
        event_id = event["id"]
        url = f"{BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "batter_home_runs",
            "bookmakers": ",".join(BOOKMAKERS),
            "oddsFormat": "american",
        }
        resp = requests.get(url, params=params)
        if resp.status_code != 200:
            continue

        data = resp.json()
        for bookmaker in data.get("bookmakers", []):
            book = bookmaker["key"]
            for market in bookmaker.get("markets", []):
                if market["key"] != "batter_home_runs":
                    continue
                for outcome in market.get("outcomes", []):
                    all_props.append({
                        "batter_name": outcome["name"],
                        "game_id": event_id,
                        "home_team": event.get("home_team"),
                        "away_team": event.get("away_team"),
                        "book": book,
                        "odds": outcome["price"],
                        "point": outcome.get("point"),
                    })

    print(f"[odds_fetcher] HR prop lines fetched: {len(all_props)} outcomes")
    return all_props


def _fetch_today_events(date_str: str) -> list[dict]:
    url = f"{BASE_URL}/sports/baseball_mlb/events"
    params = {"apiKey": ODDS_API_KEY, "dateFormat": "iso"}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    events = resp.json()
    return [e for e in events if e.get("commence_time", "")[:10] == date_str]


def _save_raw(raw: list[dict]) -> None:
    os.makedirs("data/odds", exist_ok=True)
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    path = f"data/odds/{date_str}.json"
    with open(path, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"[odds_fetcher] Saved raw odds → {path}")


if __name__ == "__main__":
    games = fetch_odds()
    print(f"\nFound {len(games)} games:\n")
    for g in games:
        bl = g["best_lines"]
        home_ml = bl["home_ml"]["odds"] if bl["home_ml"] else "N/A"
        away_ml = bl["away_ml"]["odds"] if bl["away_ml"] else "N/A"
        total = bl["over"]["line"] if bl["over"] else "N/A"
        print(f"  {g['away_team']} @ {g['home_team']}")
        print(f"    ML:    {away_ml} / {home_ml}")
        print(f"    Total: {total}")
        print(f"    Time:  {g['commence_time']}")
        print()
