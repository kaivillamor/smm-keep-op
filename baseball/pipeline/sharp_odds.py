"""
SharpAPI (sharpapi.io) client — real sportsbook odds for player props.

Free tier: 12 requests/min, 60s data delay, DraftKings + FanDuel only.
We space requests to stay under the rate limit and paginate the odds feed.

Primary use: fetch 1+ hit odds (player_hits market, Over 0.5) so the hit
pipeline can price legs on real book-implied probability instead of manual entry.
"""
import os
import time
import unicodedata

import requests
from dotenv import load_dotenv

load_dotenv()

SHARPAPI_KEY = os.getenv("SHARPAPI_KEY")
_BASE = "https://api.sharpapi.io/api/v1"

# Free tier is 12 req/min → one request every 5s. Add margin so bursts never trip it.
_MIN_INTERVAL = 5.2
_PAGE_SIZE    = 50
_MAX_PAGES    = 20          # safety cap; a full MLB slate of props is well under this
_TIMEOUT      = 20
_last_call    = 0.0


def _throttle() -> None:
    """Block just long enough to keep requests under the free-tier rate limit."""
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _get(path: str, params: dict) -> dict:
    _throttle()
    headers = {"X-API-Key": SHARPAPI_KEY, "Accept": "application/json"}
    resp = requests.get(f"{_BASE}/{path}", params=params, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _paginate(path: str, params: dict) -> list[dict]:
    """Walk the paginated odds feed via offset until has_more is false."""
    rows, offset = [], 0
    for _ in range(_MAX_PAGES):
        page = _get(path, {**params, "offset": offset, "limit": _PAGE_SIZE})
        rows += page.get("data", [])
        pg = page.get("pagination", {})
        if not pg.get("has_more"):
            break
        offset = pg.get("next_offset", offset + _PAGE_SIZE)
    return rows


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation so 'Julio Rodríguez' == 'Julio Rodriguez'
    and 'J.P. Crawford' == 'JP Crawford' — for matching book names to our lineups."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


# Restrict to a single book so a 2-leg parlay is actually placeable (you can't
# parlay a DraftKings leg with a FanDuel leg). Default FanDuel — the only book the
# user bets. Pass book="draftkings" or book=None (best-of-all) to widen coverage.
DEFAULT_BOOK = "fanduel"


def _fetch_over05(market: str, label: str, book: str | None,
                  fallback: str | None = None, relabel_as_primary: bool = False) -> dict:
    """
    Shared fetch for '1+ of stat' player props: the Over-0.5 line of `market`.
    Returns {normalized_player_name: {player, odds, implied, book, line}}.

    `book` (default FanDuel) is the preferred sportsbook. `fallback` lets a player
    missing a `book` line borrow another book's price (primary always beats fallback).
    With `relabel_as_primary`, a borrowed price is reported as the primary book's —
    used for HR parlays: FanDuel offers the market on its app so the parlay is
    placeable there regardless; DraftKings' near-identical number is just a stand-in
    for the price we couldn't scrape, so we call it FanDuel and move on.
    `book=None` keeps the best price across all books.
    """
    if not SHARPAPI_KEY:
        print(f"[sharp_odds] No SHARPAPI_KEY set — skipping {label} odds")
        return {}

    try:
        rows = _paginate("odds", {"league": "mlb", "market": market})
    except Exception as e:
        print(f"[sharp_odds] {label} fetch failed: {e}")
        return {}

    allowed = {book, fallback} - {None} if book else None  # None ⇒ all books

    def _tier(sb: str) -> int:
        """0 = preferred book, 1 = fallback/other. Lower wins."""
        return 0 if (book and sb == book) else 1

    result: dict = {}
    for r in rows:
        if r.get("line") != 0.5 or r.get("selection_type") != "over":
            continue  # Over 0.5 = P(1+); skip 1.5/2.5 lines and unders
        sb = r.get("sportsbook")
        if allowed is not None and sb not in allowed:
            continue
        name = r.get("player_name") or r.get("selection")
        if not name or r.get("odds_american") is None:
            continue
        key  = normalize_name(name)
        prev = result.get(key)
        cur_tier = _tier(sb)
        # Prefer the primary book; within the same tier take the best (highest) price.
        take = (prev is None
                or cur_tier < prev["_tier"]
                or (cur_tier == prev["_tier"] and r["odds_american"] > prev["odds"]))
        if take:
            result[key] = {
                "player":  name,
                "odds":    r["odds_american"],
                "implied": r.get("odds_probability"),
                "book":    book if (relabel_as_primary and book) else sb,
                "_tier":   cur_tier,
                "line":    r.get("line"),
            }

    for v in result.values():
        v.pop("_tier", None)  # internal only — drop before returning

    if book:
        book_label = f"{book}, {fallback} fallback" if fallback else book
    else:
        book_label = "best-of-all-books"
    print(f"[sharp_odds] {len(result)} players with 1+ {label} odds "
          f"[{book_label}] ({len(rows)} rows scanned)")
    return result


def fetch_hit_prop_odds(book: str | None = DEFAULT_BOOK) -> dict:
    """1+ hit odds (player_hits, Over 0.5), FanDuel-only — EV and single-book
    placeability matter here, so no cross-book fallback."""
    return _fetch_over05("player_hits", "hit", book)


def fetch_hr_prop_odds(book: str | None = DEFAULT_BOOK) -> dict:
    """1+ home run odds (player_home_runs, Over 0.5). FanDuel preferred; if FanDuel
    lacks a line, borrow DraftKings' price but report it as FanDuel — the parlay is
    placeable on the FanDuel app regardless, we just couldn't scrape that number."""
    fallback = "draftkings" if book == "fanduel" else None
    return _fetch_over05("player_home_runs", "HR", book,
                         fallback=fallback, relabel_as_primary=True)


if __name__ == "__main__":
    import sys
    _book = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BOOK
    board = fetch_hit_prop_odds(book=None if _book == "all" else _book)
    for k, v in sorted(board.items(), key=lambda kv: kv[1]["implied"] or 0, reverse=True):
        o = v["odds"]
        print(f"  {v['player']:24} {v['book']:11} {'+' if o > 0 else ''}{o:<5} "
              f"implied {v['implied']}")
