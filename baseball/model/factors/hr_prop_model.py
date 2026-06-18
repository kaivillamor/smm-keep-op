HR_GATE_THRESHOLD = 65.0
RECENT_DAYS = 14

# xwOBA normalization bounds: floor → 0 pts, ceiling → 100 pts
_XWOBA_FLOOR = 0.150
_XWOBA_CEIL = 0.700


def score_batter_hr_props(
    season_stats: dict,
    recent_stats: dict,
    batter_zones: dict,
    pitcher_zones: dict,
) -> dict:
    """
    Scores a batter on three HR prop metrics (each 0-100).
    Passes the gate only when all three >= HR_GATE_THRESHOLD.

    season_stats  : from fetch_batter_statcast_season (sweet_spot_percent)
    recent_stats  : from fetch_batter_recent_stats    (hard_hit_percent, last N days)
    batter_zones  : from fetch_batter_zone_stats      ({zone_id: xwoba})
    pitcher_zones : from fetch_pitcher_zone_tendencies({zone_id: frequency})
    """
    scores = {
        "sweet_spot": _sweet_spot_score(season_stats),
        "recent_hard_contact": _hard_contact_score(recent_stats),
        "zone_fit": _zone_fit_score(batter_zones, pitcher_zones),
    }
    scores["passes_gate"] = _check_gate(scores)
    return scores


# ── individual metric scorers ─────────────────────────────────────────────────

def _sweet_spot_score(season_stats: dict) -> float | None:
    pct = season_stats.get("sweet_spot_percent")
    if pct is None:
        return None
    return round(float(pct), 1)


def _hard_contact_score(recent_stats: dict) -> float | None:
    pct = recent_stats.get("hard_hit_percent")
    if pct is None:
        return None
    return round(float(pct), 1)


def _zone_fit_score(batter_zones: dict, pitcher_zones: dict) -> float | None:
    """
    Weighted dot product: for each zone, batter_strength (0-100) × pitcher_frequency.
    Batter strength is the zone's xwOBA normalized between _XWOBA_FLOOR and _XWOBA_CEIL.
    Pitcher frequency weights each zone's contribution (sums to 1.0 across common zones).
    """
    if not batter_zones or not pitcher_zones:
        return None

    common_zones = set(batter_zones.keys()) & set(pitcher_zones.keys())
    if not common_zones:
        return None

    total_freq = sum(pitcher_zones[z] for z in common_zones)
    if total_freq == 0:
        return None

    weighted_sum = 0.0
    for zone in common_zones:
        xwoba = batter_zones[zone]
        freq = pitcher_zones[zone]
        strength = (xwoba - _XWOBA_FLOOR) / (_XWOBA_CEIL - _XWOBA_FLOOR)
        strength = max(0.0, min(1.0, strength)) * 100
        weighted_sum += strength * (freq / total_freq)

    return round(weighted_sum, 1)


# ── gate ──────────────────────────────────────────────────────────────────────

def _check_gate(scores: dict, threshold: float = HR_GATE_THRESHOLD) -> bool:
    """All three metric scores must be non-None and >= threshold."""
    metric_keys = ("sweet_spot", "recent_hard_contact", "zone_fit")
    return all(
        scores.get(k) is not None and scores[k] >= threshold
        for k in metric_keys
    )
