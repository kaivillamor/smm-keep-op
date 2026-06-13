import math

# Compass bearing FROM home plate TOWARD center field for each outdoor park.
# Used to determine whether wind blows toward or away from the outfield.
# Note: OpenWeather reports where wind comes FROM, so we invert it below.
CF_DIRECTION = {
    "Arizona Diamondbacks":  45,   # Chase Field
    "Atlanta Braves":        90,   # Truist Park
    "Baltimore Orioles":     20,   # Camden Yards
    "Boston Red Sox":        45,   # Fenway Park
    "Chicago Cubs":          85,   # Wrigley — famous for W wind blowing out
    "Chicago White Sox":     355,  # Guaranteed Rate Field
    "Cincinnati Reds":       45,   # Great American Ball Park
    "Cleveland Guardians":   10,   # Progressive Field
    "Colorado Rockies":      15,   # Coors Field
    "Detroit Tigers":        330,  # Comerica Park
    "Kansas City Royals":    20,   # Kauffman Stadium
    "Los Angeles Angels":    315,  # Angel Stadium
    "Los Angeles Dodgers":   20,   # Dodger Stadium
    "Minnesota Twins":       315,  # Target Field
    "New York Mets":         50,   # Citi Field
    "New York Yankees":      15,   # Yankee Stadium
    "Oakland Athletics":     300,  # Sacramento (temp)
    "Philadelphia Phillies": 45,   # Citizens Bank Park
    "Pittsburgh Pirates":    330,  # PNC Park
    "San Diego Padres":      310,  # Petco Park
    "San Francisco Giants":  310,  # Oracle Park — Bay wind typically blows IN
    "St. Louis Cardinals":   20,   # Busch Stadium
    "Washington Nationals":  15,   # Nationals Park
}

TEMP_BASELINE_F = 72.0
WIND_RUN_FACTOR = 0.005   # ~0.5% change in run total per mph of aligned wind
TEMP_RUN_FACTOR = 0.002   # ~2% change in run total per 10°F deviation


def get_weather_adjustment(weather: dict, team: str) -> float:
    """
    Returns a run total adjustment (+ = more runs expected, - = fewer).
    E.g. +0.4 means add 0.4 to the base expected run total.
    Returns 0.0 for domed parks or missing data.
    """
    if not weather or not weather.get("is_outdoor"):
        return 0.0

    wind_adj = wind_effect(
        speed_mph=weather.get("wind_speed_mph"),
        wind_from_deg=weather.get("wind_deg"),
        team=team,
    )
    temp_adj = temp_effect(weather.get("temp_f"))

    return round(wind_adj + temp_adj, 3)


def wind_effect(speed_mph: float, wind_from_deg: float, team: str) -> float:
    if speed_mph is None or wind_from_deg is None:
        return 0.0

    cf_deg = CF_DIRECTION.get(team)
    if cf_deg is None:
        return 0.0

    # OpenWeather gives direction wind comes FROM — invert to get where it's going
    wind_toward_deg = (wind_from_deg + 180) % 360

    angle_diff = abs(wind_toward_deg - cf_deg)
    if angle_diff > 180:
        angle_diff = 360 - angle_diff

    # 1.0 = straight out to CF (more runs), -1.0 = straight in (fewer runs)
    alignment = math.cos(math.radians(angle_diff))

    return alignment * speed_mph * WIND_RUN_FACTOR


def temp_effect(temp_f: float) -> float:
    if temp_f is None:
        return 0.0
    return ((temp_f - TEMP_BASELINE_F) / 10) * TEMP_RUN_FACTOR


if __name__ == "__main__":
    examples = [
        ("Chicago Cubs",        {"is_outdoor": True, "temp_f": 78, "wind_speed_mph": 18, "wind_deg": 270, "wind_direction": "W"}),
        ("San Francisco Giants",{"is_outdoor": True, "temp_f": 58, "wind_speed_mph": 14, "wind_deg": 300, "wind_direction": "WNW"}),
        ("Colorado Rockies",    {"is_outdoor": True, "temp_f": 88, "wind_speed_mph": 8,  "wind_deg": 200, "wind_direction": "SSW"}),
        ("New York Yankees",    {"is_outdoor": True, "temp_f": 65, "wind_speed_mph": 10, "wind_deg": 195, "wind_direction": "SSW"}),
        ("Tampa Bay Rays",      {"is_outdoor": False}),
    ]

    print("Weather adjustment examples (run total delta):\n")
    for team, weather in examples:
        adj = get_weather_adjustment(weather, team)
        w_adj = wind_effect(weather.get("wind_speed_mph"), weather.get("wind_deg"), team)
        t_adj = temp_effect(weather.get("temp_f"))
        if weather.get("is_outdoor"):
            print(f"  {team:<26} total={adj:+.3f}  (wind={w_adj:+.3f}, temp={t_adj:+.3f})")
            print(f"    {weather.get('wind_speed_mph')} mph {weather.get('wind_direction')} | {weather.get('temp_f')}°F")
        else:
            print(f"  {team:<26} DOME — no adjustment")
        print()
