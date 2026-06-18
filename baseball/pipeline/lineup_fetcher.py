import json
import os
import requests
from datetime import datetime, timezone, timedelta

MLB_API = "https://statsapi.mlb.com/api/v1"

# MLB schedules are always in Eastern Time.
# Offset from UTC: EDT = UTC-4 (summer), EST = UTC-5 (winter).
# Using -5 as a safe floor — in the worst case we're off by an hour near midnight ET,
# but it prevents us from accidentally pulling tomorrow's games when UTC ticks past midnight.
_ET_OFFSET = timedelta(hours=5)


def fetch_lineups() -> dict:
    et_now = datetime.now(timezone.utc) - _ET_OFFSET
    date_str = et_now.strftime("%Y-%m-%d")
    games = _fetch_today_games(date_str)

    lineups = {}
    confirmed_count = 0

    for game in games:
        game_pk = game["gamePk"]
        home_team = game["teams"]["home"]["team"]["name"]
        away_team = game["teams"]["away"]["team"]["name"]

        lineup_data = _fetch_game_lineup(game_pk)
        confirmed = lineup_data.get("confirmed", False)
        if confirmed:
            confirmed_count += 1

        lineups[str(game_pk)] = {
            "home_team": home_team,
            "away_team": away_team,
            "home_lineup": lineup_data.get("home", []),
            "away_lineup": lineup_data.get("away", []),
            "confirmed": confirmed,
            "commence_time": game.get("gameDate"),
        }

    _save(lineups, date_str)
    print(f"[lineup_fetcher] {len(lineups)} games | {confirmed_count} with confirmed lineups")
    return lineups


def _fetch_today_games(date_str: str) -> list[dict]:
    resp = requests.get(f"{MLB_API}/schedule", params={"sportId": 1, "date": date_str, "hydrate": "team"})
    resp.raise_for_status()
    dates = resp.json().get("dates", [])
    if not dates:
        return []
    return dates[0].get("games", [])


def _fetch_game_lineup(game_pk: int) -> dict:
    # Primary: /lineups endpoint (data is nested under a "lineups" key)
    resp = requests.get(f"{MLB_API}/game/{game_pk}/lineups")
    if resp.status_code == 200:
        data = resp.json()
        nested = data.get("lineups", data)  # handle both nested and flat responses
        home_players = nested.get("homePlayers", [])
        away_players = nested.get("awayPlayers", [])
        if home_players or away_players:
            return {
                "confirmed": True,
                "home": [_parse_player(p) for p in home_players],
                "away": [_parse_player(p) for p in away_players],
            }

    # Fallback: /boxscore works for in-progress and completed games
    return _fetch_lineup_from_boxscore(game_pk)


def _fetch_lineup_from_boxscore(game_pk: int) -> dict:
    resp = requests.get(f"{MLB_API}/game/{game_pk}/boxscore")
    if resp.status_code != 200:
        return {"confirmed": False, "home": [], "away": []}

    data = resp.json()
    teams = data.get("teams", {})

    def extract_batting_order(side: dict) -> list[dict]:
        players = side.get("players", {})
        order = side.get("battingOrder", [])
        result = []
        for pid in order:
            player_key = f"ID{pid}"
            p = players.get(player_key, {})
            person = p.get("person", {})
            result.append({
                "id": person.get("id"),
                "name": person.get("fullName"),
                "position": p.get("position", {}).get("abbreviation"),
                "batting_order": p.get("battingOrder"),
            })
        return result

    home = extract_batting_order(teams.get("home", {}))
    away = extract_batting_order(teams.get("away", {}))

    if not home and not away:
        return {"confirmed": False, "home": [], "away": []}

    return {"confirmed": True, "home": home, "away": away}


def _parse_player(player: dict) -> dict:
    return {
        "id": player.get("id"),
        "name": player.get("fullName"),
        "position": player.get("primaryPosition", {}).get("abbreviation"),
        "batting_order": player.get("battingOrder"),
    }


def fetch_team_roster(team_id: int) -> list[dict]:
    """Fallback — returns active roster when lineups aren't posted yet."""
    resp = requests.get(
        f"{MLB_API}/teams/{team_id}/roster",
        params={"rosterType": "active"},
    )
    resp.raise_for_status()
    roster = resp.json().get("roster", [])
    return [_parse_player(p.get("person", {})) for p in roster]


def _save(data: dict, date_str: str) -> None:
    os.makedirs("data/lineups", exist_ok=True)
    path = f"data/lineups/{date_str}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[lineup_fetcher] Saved → {path}")


if __name__ == "__main__":
    lineups = fetch_lineups()
    print()
    for game_pk, game in lineups.items():
        status = "CONFIRMED" if game["confirmed"] else "not yet posted"
        print(f"  {game['away_team']} @ {game['home_team']} — lineups: {status}")
        if game["confirmed"]:
            home = [p["name"] for p in game["home_lineup"][:3]]
            away = [p["name"] for p in game["away_lineup"][:3]]
            print(f"    Home 1-3: {', '.join(home)}")
            print(f"    Away 1-3: {', '.join(away)}")
        print()
