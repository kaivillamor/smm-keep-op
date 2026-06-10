import os
import requests
from dotenv import load_dotenv

load_dotenv()

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
BASE_URL = "https://api.openweathermap.org/data/2.5/forecast"

BALLPARK_COORDS = {
    # team_abbr: (lat, lon)
}


def fetch_weather() -> dict:
    pass


def fetch_park_weather(team: str) -> dict:
    pass
