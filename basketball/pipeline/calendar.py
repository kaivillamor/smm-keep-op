"""NBA slate calendar: which day a game belongs to, and which part of the season."""
from datetime import datetime, timezone, timedelta

# A fixed offset, not a DST-aware zone: the extra hour keeps a late tip on its slate.
_ET_OFFSET = timedelta(hours=5)


def slate_date() -> str:
    """Today's NBA slate date (Eastern), as YYYY-MM-DD."""
    return (datetime.now(timezone.utc) - _ET_OFFSET).strftime("%Y-%m-%d")


def game_slate_date(tipoff: str | None) -> str:
    """The slate date a tip-off belongs to: the key of a prediction row, never the run's
    own date. Falls back to today's slate when the tip-off is missing or unparseable."""
    if not tipoff:
        return slate_date()
    try:
        dt = datetime.fromisoformat(str(tipoff).replace("Z", "+00:00"))
    except ValueError:
        return slate_date()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc) - _ET_OFFSET).strftime("%Y-%m-%d")


# Season boundaries, oldest first, as explicit date ranges (a season spans two years).
SEASONS = (
    {
        "label":            "2026-27",
        "preseason_start":  "2026-09-28",
        "regular_start":    "2026-10-20",
        "postseason_start": None,          # 2027 playoff start — fill in when scheduled
        "season_end":       None,
    },
)


def season_phase(game_date: str | None) -> str | None:
    """'preseason' | 'regular' | 'postseason' from SEASONS, or None outside every recorded
    season. Fallback only: prefer the per-game `phase` from the scoreboard."""
    if not game_date:
        return None
    d = str(game_date)
    for s in SEASONS:
        if d < s["preseason_start"]:
            continue
        if s["season_end"] and d > s["season_end"]:
            continue
        if d < s["regular_start"]:
            return "preseason"
        post = s["postseason_start"]
        if post is None:
            return "regular"
        return "postseason" if d >= post else "regular"
    return None
