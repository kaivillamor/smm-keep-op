PARK_FACTORS = {
    # team_abbr: runs_per_game_index (1.0 = neutral)
}


def get_park_factor(team: str) -> float:
    return PARK_FACTORS.get(team, 1.0)
