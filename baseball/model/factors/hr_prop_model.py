HR_GATE_THRESHOLD    = 65.0   # threshold for sweet spot, hard contact, zone fit
BARREL_THRESHOLD     = 11.0   # barrel rate % that qualifies alone (top ~20% of MLB; avg ~7%)
BARREL_SOFT          = 8.0    # lower barrel threshold when facing a homer-prone pitcher
HR_FB_THRESHOLD      = 15.0   # pitcher HR/FB rate % — above this is homer-prone (league avg ~11%)
RECENT_DAYS          = 14
HR_PARLAY_STAKE      = 10.0   # flat $ per 2-leg HR parlay (both batters to hit 1+ HR)

# xwOBA normalization bounds: floor → 0 pts, ceiling → 100 pts
_XWOBA_FLOOR = 0.150
_XWOBA_CEIL  = 0.700


def score_batter_hr_props(
    season_stats: dict,
    recent_stats: dict,
    batter_zones: dict,
    pitcher_zones: dict,
    pitcher_hr_fb: float | None = None,
) -> dict:
    """
    Scores a batter on four HR prop metrics and applies the gate.

    Gate logic (any condition qualifies):
      1. Barrel rate >= BARREL_THRESHOLD               (elite contact quality alone)
      2. Sweet Spot >= HR_GATE_THRESHOLD
         AND Hard Contact >= HR_GATE_THRESHOLD          (angle + power together)
      3. Zone Fit >= HR_GATE_THRESHOLD
         AND Hard Contact >= HR_GATE_THRESHOLD          (great matchup + power together)
      4. Pitcher HR/FB >= HR_FB_THRESHOLD
         AND Barrel >= BARREL_SOFT                      (homer-prone pitcher + decent pop)

    season_stats  : from fetch_batter_statcast_season
    recent_stats  : from fetch_batter_recent_stats (14-day window; falls back to season)
    batter_zones  : from fetch_batter_zone_stats   ({zone_id: xwoba})
    pitcher_zones : from fetch_pitcher_zone_tendencies ({zone_id: frequency})
    """
    scores = {
        "barrel_rate":         _barrel_rate_score(season_stats),
        "sweet_spot":          _sweet_spot_score(season_stats),
        "recent_hard_contact": _hard_contact_score(recent_stats, season_stats),
        "zone_fit":            _zone_fit_score(batter_zones, pitcher_zones),
        "pitcher_hr_fb":       round(pitcher_hr_fb, 1) if pitcher_hr_fb is not None else None,
    }
    passes = _check_gate(scores)
    scores["passes_gate"]    = passes
    scores["gate_triggered"] = _which_gate(scores) if passes else None
    return scores


# ── individual metric scorers ─────────────────────────────────────────────────

def _barrel_rate_score(season_stats: dict) -> float | None:
    rate = season_stats.get("barrel_batted_rate")
    if rate is None:
        return None
    return round(float(rate), 1)


def _sweet_spot_score(season_stats: dict) -> float | None:
    pct = season_stats.get("sweet_spot_percent")
    if pct is None:
        return None
    return round(float(pct), 1)


def _hard_contact_score(recent_stats: dict, season_stats: dict) -> float | None:
    """Prefer the 14-day window; fall back to season only when recent is genuinely absent.

    Must test `is None`, not falsiness: a batter who made contact but hit nothing hard
    scores a legitimate 0.0, and `or` treated that as missing and substituted the season
    figure — erasing exactly the cold streak this metric exists to catch.
    """
    pct = recent_stats.get("hard_hit_percent")
    if pct is None:
        pct = season_stats.get("hard_hit_percent")
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
        freq  = pitcher_zones[zone]
        strength = (xwoba - _XWOBA_FLOOR) / (_XWOBA_CEIL - _XWOBA_FLOOR)
        strength = max(0.0, min(1.0, strength)) * 100
        weighted_sum += strength * (freq / total_freq)

    return round(weighted_sum, 1)


# ── gate ──────────────────────────────────────────────────────────────────────

def _which_gate(scores: dict) -> str | None:
    """Returns a short label for the first gate condition that triggered."""
    barrel       = scores.get("barrel_rate")
    sweet_spot   = scores.get("sweet_spot")
    hard_contact = scores.get("recent_hard_contact")
    zone_fit     = scores.get("zone_fit")
    pitcher_hrfb = scores.get("pitcher_hr_fb")
    t = HR_GATE_THRESHOLD

    if barrel is not None and barrel >= BARREL_THRESHOLD:
        return "barrel"
    if sweet_spot is not None and sweet_spot >= t and hard_contact is not None and hard_contact >= t:
        return "sweet_hc"
    if zone_fit is not None and zone_fit >= t and hard_contact is not None and hard_contact >= t:
        return "zone_hc"
    if pitcher_hrfb is not None and pitcher_hrfb >= HR_FB_THRESHOLD and barrel is not None and barrel >= BARREL_SOFT:
        return "pitcher_hrfb"
    return None


def _check_gate(scores: dict) -> bool:
    """
    Four ways to pass (thresholds are the module constants, not hardcoded):
      1. Barrel >= BARREL_THRESHOLD (11%)                      elite contact quality alone
      2. Sweet Spot >= 65% AND Hard Contact >= 65%             angle + power
      3. Zone Fit >= 65% AND Hard Contact >= 65%               matchup + power
      4. Pitcher HR/FB >= 15% AND Barrel >= BARREL_SOFT (8%)   homer-prone pitcher + pop
    """
    barrel       = scores.get("barrel_rate")
    sweet_spot   = scores.get("sweet_spot")
    hard_contact = scores.get("recent_hard_contact")
    zone_fit     = scores.get("zone_fit")
    pitcher_hrfb = scores.get("pitcher_hr_fb")

    t = HR_GATE_THRESHOLD

    if barrel is not None and barrel >= BARREL_THRESHOLD:
        return True

    if (sweet_spot   is not None and sweet_spot   >= t and
            hard_contact is not None and hard_contact >= t):
        return True

    if (zone_fit     is not None and zone_fit     >= t and
            hard_contact is not None and hard_contact >= t):
        return True

    if (pitcher_hrfb is not None and pitcher_hrfb >= HR_FB_THRESHOLD and
            barrel   is not None and barrel        >= BARREL_SOFT):
        return True

    return False


def rank_score(scores: dict) -> float:
    """
    Composite ranking score (0–100) for sorting candidates after gate filtering.
    Barrel rate carries the most weight — it's the best single HR predictor.
    Hard contact and sweet spot add signal; zone fit and pitcher_hrfb are bonuses.

    Missing metrics are EXCLUDED and the remaining weights renormalized, rather than
    scored as 0. Treating an unavailable zone fit or HR/FB proxy as zero made it the
    worst possible value, so candidates ranked lower for having incomplete data instead
    of for being worse hitters — a data-availability bias, not a skill signal.
    """
    # (normalized 0–1 value, weight) — only for metrics we actually have
    parts: list[tuple[float, float]] = []

    barrel = scores.get("barrel_rate")
    if barrel is not None:
        parts.append((min(barrel / 20.0, 1.0), 0.40))        # 20% barrel ≈ elite ceiling

    hard_contact = scores.get("recent_hard_contact")
    if hard_contact is not None:
        parts.append((min(hard_contact / 75.0, 1.0), 0.25))

    sweet_spot = scores.get("sweet_spot")
    if sweet_spot is not None:
        parts.append((min(sweet_spot / 75.0, 1.0), 0.20))

    zone_fit = scores.get("zone_fit")
    if zone_fit is not None:
        parts.append((min(zone_fit / 100.0, 1.0), 0.10))

    pitcher_hrfb = scores.get("pitcher_hr_fb")
    if pitcher_hrfb is not None:
        parts.append((min(max(pitcher_hrfb - 10.0, 0.0) / 15.0, 1.0), 0.05))  # bonus above 10%

    total_weight = sum(w for _, w in parts)
    if not total_weight:
        return 0.0
    return round(sum(v * w for v, w in parts) / total_weight * 100, 1)
