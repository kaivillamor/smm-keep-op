# 3-year FanGraphs park factors (2022–2024 averages). 1.0 = perfectly neutral.
# Values above 1.0 = hitter-friendly, below 1.0 = pitcher-friendly.
# Update annually — these drift as fences move and rosters change.

PARK_FACTORS = {
    # team: (runs_factor, hr_factor)
    "Arizona Diamondbacks":  (1.05, 1.06),
    "Atlanta Braves":        (1.04, 1.05),
    "Baltimore Orioles":     (1.04, 1.07),
    "Boston Red Sox":        (1.07, 1.05),  # Fenway wall helps doubles, not HRs
    "Chicago Cubs":          (1.06, 1.04),  # Wind-dependent — adjusted in weather layer
    "Chicago White Sox":     (1.02, 1.04),
    "Cincinnati Reds":       (1.12, 1.15),  # Great American BP — very hitter-friendly
    "Cleveland Guardians":   (0.97, 0.97),
    "Colorado Rockies":      (1.37, 1.35),  # Coors — altitude makes this a massive outlier
    "Detroit Tigers":        (0.97, 0.88),  # Comerica is huge, suppresses HRs hard
    "Houston Astros":        (1.03, 1.04),
    "Kansas City Royals":    (1.01, 0.98),
    "Los Angeles Angels":    (1.00, 0.98),
    "Los Angeles Dodgers":   (0.96, 0.96),
    "Miami Marlins":         (0.94, 0.93),
    "Milwaukee Brewers":     (1.04, 1.06),
    "Minnesota Twins":       (1.00, 1.00),
    "New York Mets":         (0.96, 0.96),
    "New York Yankees":      (1.05, 1.15),  # Short RF porch inflates HR factor significantly
    "Oakland Athletics":     (0.95, 0.91),  # Sacramento (temp) — update for LV
    "Philadelphia Phillies": (1.08, 1.10),
    "Pittsburgh Pirates":    (0.97, 0.94),
    "San Diego Padres":      (0.94, 0.91),
    "San Francisco Giants":  (0.93, 0.87),  # Marine layer + large foul territory
    "Seattle Mariners":      (0.95, 0.92),
    "St. Louis Cardinals":   (0.99, 0.97),
    "Tampa Bay Rays":        (0.95, 0.96),
    "Texas Rangers":         (1.03, 1.04),
    "Toronto Blue Jays":     (1.02, 1.04),
    "Washington Nationals":  (0.99, 1.01),
}


def get_park_factor(team: str) -> tuple[float, float]:
    """Returns (runs_factor, hr_factor) for the given home team."""
    return PARK_FACTORS.get(team, (1.0, 1.0))


def get_runs_factor(team: str) -> float:
    return get_park_factor(team)[0]


def get_hr_factor(team: str) -> float:
    return get_park_factor(team)[1]


if __name__ == "__main__":
    print("Park factors ranked by run-scoring (hitter-friendly → pitcher-friendly):\n")
    ranked = sorted(PARK_FACTORS.items(), key=lambda x: x[1][0], reverse=True)
    for team, (runs, hr) in ranked:
        bar = "+" * int((runs - 0.85) * 40)
        print(f"  {team:<26} runs={runs:.2f}  hr={hr:.2f}  {bar}")
