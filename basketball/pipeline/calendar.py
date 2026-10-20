"""
NBA slate calendar — which day a game belongs to, and which part of the season.

Its own module rather than living inside a stats fetcher (where the baseball twin keeps
it) because `output/research.py` needs it before any ESPN code exists, and a logging
module should not have to import a stats fetcher to learn what day it is.

Both functions here exist because of bugs that cost real data in baseball. Read
PROJECT.md §29 before changing either.
"""
from datetime import datetime, timezone, timedelta

# A fixed offset, not a DST-aware zone, and deliberately so: this only ever answers
# "which calendar day is this slate", and the extra hour of grace keeps a late tip on the
# right day. The baseball twin documents the same choice.
_ET_OFFSET = timedelta(hours=5)


def slate_date() -> str:
    """Today's NBA slate date (Eastern), as YYYY-MM-DD."""
    return (datetime.now(timezone.utc) - _ET_OFFSET).strftime("%Y-%m-%d")


def game_slate_date(tipoff: str | None) -> str:
    """The slate date a given tip-off belongs to.

    THIS is what keys a prediction row, not the date collection happened to run. Baseball
    used `date.today()` inside UNIQUE(date, ...), so a game scored either side of UTC
    midnight was written twice under two dates — 678 duplicate rows, 20% of the corpus,
    and every confidence interval ~12% too narrow until it was found (§29).

    NBA is more exposed to this than MLB, not less. Baseball collected 19:00-02:00 UTC;
    a 10:30pm ET tip is 02:30 UTC the NEXT day, so a larger share of the slate crosses
    midnight. Deriving the date from tip-off makes it a property of the game, which no
    clock can move.

    Falls back to today's slate when the tip-off is missing or unparseable, so a game with
    no start time is attributed to the current slate rather than dropped.
    """
    if not tipoff:
        return slate_date()
    try:
        dt = datetime.fromisoformat(str(tipoff).replace("Z", "+00:00"))
    except ValueError:
        return slate_date()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc) - _ET_OFFSET).strftime("%Y-%m-%d")


# Season boundaries, oldest first. An NBA season spans two calendar years, so these are
# explicit date ranges — a calendar-year key would put January games in the wrong season.
#
# `postseason_start` is left None until the date is published. season_phase() then returns
# None rather than guessing, for the same reason the baseball twin does: an unrecorded
# boundary is unknown, not "regular", and a confident wrong label is how postseason rows
# end up pooled into a model corpus.
SEASONS = (
    {
        "label":            "2026-27",
        "preseason_start":  "2026-09-28",
        "regular_start":    "2026-10-21",
        "postseason_start": None,          # 2027 playoff start — fill in when scheduled
        "season_end":       None,
    },
)


def season_phase(game_date: str | None) -> str | None:
    """'preseason' | 'regular' | 'postseason', or None when the date is outside every
    recorded season or past an unrecorded boundary.

    Preseason is the NBA-specific reason this column exists on day one, and it matters
    more here than the baseball equivalent did. Preseason minutes are not real minutes —
    starters play roughly 15 — and minutes are the single biggest driver of every prop in
    this model. Pooling preseason rows into the corpus would not add noise so much as
    teach the model a different sport.
    """
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
            # Regular season has started and we don't know when it ends. Saying "regular"
            # past the real boundary would be the exact mislabelling this guards against,
            # so only claim it while the date is plausibly still in-season.
            return "regular"
        return "postseason" if d >= post else "regular"
    return None
