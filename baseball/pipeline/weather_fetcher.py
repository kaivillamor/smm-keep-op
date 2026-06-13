import json
import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
BASE_URL = "https://api.openweathermap.org/data/2.5/forecast"

# (lat, lon, is_outdoor) — retractable/fixed domes marked False
BALLPARK_COORDS = {
    "Arizona Diamondbacks":     (33.4455, -112.0667, False),
    "Atlanta Braves":           (33.8908,  -84.4678, True),
    "Baltimore Orioles":        (39.2839,  -76.6216, True),
    "Boston Red Sox":           (42.3467,  -71.0972, True),
    "Chicago Cubs":             (41.9484,  -87.6553, True),
    "Chicago White Sox":        (41.8300,  -87.6338, True),
    "Cincinnati Reds":          (39.0979,  -84.5082, True),
    "Cleveland Guardians":      (41.4962,  -81.6852, True),
    "Colorado Rockies":         (39.7559, -104.9942, True),
    "Detroit Tigers":           (42.3390,  -83.0485, True),
    "Houston Astros":           (29.7573,  -95.3555, False),
    "Kansas City Royals":       (39.0514,  -94.4803, True),
    "Los Angeles Angels":       (33.8003, -117.8827, True),
    "Los Angeles Dodgers":      (34.0739, -118.2400, True),
    "Miami Marlins":            (25.7781,  -80.2197, False),
    "Milwaukee Brewers":        (43.0280,  -87.9712, False),
    "Minnesota Twins":          (44.9817,  -93.2781, True),
    "New York Mets":            (40.7571,  -73.8458, True),
    "New York Yankees":         (40.8296,  -73.9262, True),
    "Oakland Athletics":        (38.5802, -121.5001, True),  # Sacramento (temp)
    "Philadelphia Phillies":    (39.9061,  -75.1665, True),
    "Pittsburgh Pirates":       (40.4469,  -80.0057, True),
    "San Diego Padres":         (32.7073, -117.1566, True),
    "San Francisco Giants":     (37.7786, -122.3893, True),
    "Seattle Mariners":         (47.5914, -122.3325, False),
    "St. Louis Cardinals":      (38.6226,  -90.1928, True),
    "Tampa Bay Rays":           (27.7682,  -82.6534, False),
    "Texas Rangers":            (32.7473,  -97.0828, False),
    "Toronto Blue Jays":        (43.6414,  -79.3894, False),
    "Washington Nationals":     (38.8730,  -77.0074, True),
}


def fetch_weather(games: list[dict]) -> dict:
    """Takes normalized game dicts from odds_fetcher and returns weather keyed by game id."""
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    results = {}

    for game in games:
        game_id = game["id"]
        home_team = game["home_team"]
        commence_time = game.get("commence_time")

        weather = fetch_park_weather(home_team, commence_time)
        results[game_id] = weather

    _save(results, date_str)
    outdoor = sum(1 for w in results.values() if w.get("is_outdoor"))
    print(f"[weather_fetcher] {len(results)} games | {outdoor} outdoor parks fetched")
    return results


def fetch_park_weather(team: str, game_time: str = None) -> dict:
    coords = BALLPARK_COORDS.get(team)
    if not coords:
        return {"team": team, "error": "unknown ballpark", "is_outdoor": True}

    lat, lon, is_outdoor = coords

    if not is_outdoor:
        return {"team": team, "is_outdoor": False, "note": "dome/retractable — weather not applicable"}

    if not OPENWEATHER_API_KEY:
        return {"team": team, "is_outdoor": True, "error": "missing OPENWEATHER_API_KEY"}

    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "imperial",
    }
    resp = requests.get(BASE_URL, params=params)
    resp.raise_for_status()
    forecast = resp.json()

    snapshot = _closest_forecast(forecast["list"], game_time)
    return _parse_snapshot(team, is_outdoor, snapshot)


def _closest_forecast(forecasts: list[dict], game_time: str) -> dict:
    if not game_time or not forecasts:
        return forecasts[0] if forecasts else {}

    try:
        target = datetime.fromisoformat(game_time.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return forecasts[0]

    return min(forecasts, key=lambda f: abs(f["dt"] - target))


def _parse_snapshot(team: str, is_outdoor: bool, snapshot: dict) -> dict:
    wind = snapshot.get("wind", {})
    main = snapshot.get("main", {})
    rain = snapshot.get("rain", {})
    weather_desc = snapshot.get("weather", [{}])[0]

    return {
        "team": team,
        "is_outdoor": is_outdoor,
        "temp_f": main.get("temp"),
        "feels_like_f": main.get("feels_like"),
        "humidity_pct": main.get("humidity"),
        "wind_speed_mph": wind.get("speed"),
        "wind_deg": wind.get("deg"),
        "wind_direction": _deg_to_compass(wind.get("deg")),
        "rain_3h_mm": rain.get("3h", 0),
        "condition": weather_desc.get("main"),
        "description": weather_desc.get("description"),
        "forecast_time": snapshot.get("dt_txt"),
    }


def _deg_to_compass(deg: float) -> str:
    if deg is None:
        return "unknown"
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[round(deg / 22.5) % 16]


def _save(data: dict, date_str: str) -> None:
    os.makedirs("data/weather", exist_ok=True)
    path = f"data/weather/{date_str}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[weather_fetcher] Saved → {path}")


if __name__ == "__main__":
    # Quick test — fetch weather for a few parks without needing game data
    test_teams = ["New York Yankees", "Chicago Cubs", "Tampa Bay Rays", "Colorado Rockies"]
    print()
    for team in test_teams:
        w = fetch_park_weather(team)
        if w.get("is_outdoor") is False:
            print(f"  {team}: DOME — weather skipped")
        elif w.get("error"):
            print(f"  {team}: error — {w['error']}")
        else:
            print(f"  {team}: {w.get('temp_f')}°F | wind {w.get('wind_speed_mph')} mph {w.get('wind_direction')} | {w.get('description')}")
