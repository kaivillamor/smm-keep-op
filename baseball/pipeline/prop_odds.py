"""
Player-prop odds API client; base URL and key come from .env (PROP_ODDS_BASE_URL,
PROP_ODDS_API_KEY). Twin of basketball/pipeline/prop_odds.py: a logic fix in one needs
checking in the other.
"""
import os
import time
import unicodedata

import requests
from dotenv import load_dotenv

load_dotenv()

PROP_ODDS_API_KEY = os.getenv("PROP_ODDS_API_KEY")
_BASE = os.getenv("PROP_ODDS_BASE_URL", "").rstrip("/")

# Free tier is ~12 req/min → one request every 5s. Add margin so bursts never trip it.
_MIN_INTERVAL = 5.2
_PAGE_SIZE    = 200         # the API's page cap
_MAX_PAGES    = 20          # safety cap: 20 × _PAGE_SIZE rows
_TIMEOUT      = 20
_last_call    = 0.0

_RETRY_STATUS   = {500, 502, 503, 504, 429}
_MAX_RETRIES    = 3
_RETRY_BACKOFF  = 2.0       # seconds, escalates per attempt (on top of the throttle)


def _throttle() -> None:
    """Block just long enough to keep requests under the free-tier rate limit."""
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _get(path: str, params: dict) -> dict:
    """GET one page, retrying transient gateway/timeout/rate-limit errors.
    Real errors (401 auth, 400 bad param, 404) fail fast without retrying."""
    headers  = {"X-API-Key": PROP_ODDS_API_KEY, "Accept": "application/json"}
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            resp = requests.get(f"{_BASE}/{path}", params=params,
                                 headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in _RETRY_STATUS:
                raise                      # non-transient — don't waste retries
            last_exc = e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF * (attempt + 1))
    raise last_exc                          # exhausted retries — caller returns {}


def _paginate(path: str, params: dict) -> list[dict]:
    """Walk the feed by offset until has_more is false. A later-page failure keeps the
    rows already fetched (the API 400s past a deep offset); only a first-page failure
    raises."""
    rows, offset = [], 0
    for _ in range(_MAX_PAGES):
        try:
            page = _get(path, {**params, "offset": offset, "limit": _PAGE_SIZE})
        except Exception as e:
            if not rows:
                raise
            print(f"[prop_odds] pagination stopped at offset {offset} ({e}) — "
                  f"keeping {len(rows)} rows already fetched")
            break
        rows += page.get("data", [])
        pg = page.get("pagination", {})
        if not pg.get("has_more"):
            break
        # `next_offset` can be null even with has_more=true.
        next_offset = pg.get("next_offset")
        offset = next_offset if isinstance(next_offset, int) else offset + _PAGE_SIZE
    return rows


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation so 'Julio Rodríguez' == 'Julio Rodriguez'
    and 'J.P. Crawford' == 'JP Crawford' — for matching book names to our lineups."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


# DraftKings for pricing: this provider's MLB FanDuel prices don't match the book. FanDuel
# is only a fallback, always recorded under its own name.
PREFERRED_BOOK = "draftkings"
FALLBACK_BOOK  = "fanduel"


def _fetch_over05(market: str, label: str, book: str | None,
                  fallback: str | None = None) -> dict:
    """
    Shared fetch for '1+ of stat' player props: the Over-0.5 line of `market`.
    Returns {normalized_player_name: {player, odds, implied, book, line}}. `fallback`
    fills players `book` lacks, always under the quoting book's name; `book=None` keeps
    the best price across all books.
    """
    if not PROP_ODDS_API_KEY or not _BASE:
        print(f"[prop_odds] PROP_ODDS_API_KEY / PROP_ODDS_BASE_URL not set — skipping {label} odds")
        return {}

    try:
        rows = _paginate("odds", {"league": "mlb", "market": market})
    except Exception as e:
        print(f"[prop_odds] {label} fetch failed: {e}")
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
                "book":    sb,
                "_tier":   cur_tier,
                "line":    r.get("line"),
            }

    for v in result.values():
        v.pop("_tier", None)  # internal only — drop before returning

    if book:
        book_label = f"{book}, {fallback} fallback" if fallback else book
    else:
        book_label = "best-of-all-books"
    print(f"[prop_odds] {len(result)} players with 1+ {label} odds "
          f"[{book_label}] ({len(rows)} rows scanned)")
    return result


def fetch_hit_prop_odds(book: str | None = PREFERRED_BOOK) -> dict:
    """1+ hit odds (player_hits, Over 0.5): DraftKings, else FanDuel."""
    fallback = FALLBACK_BOOK if book == PREFERRED_BOOK else None
    return _fetch_over05("player_hits", "hit", book, fallback=fallback)


def fetch_hr_prop_odds(book: str | None = PREFERRED_BOOK) -> dict:
    """1+ home run odds (player_home_runs, Over 0.5): DraftKings, else FanDuel."""
    fallback = FALLBACK_BOOK if book == PREFERRED_BOOK else None
    return _fetch_over05("player_home_runs", "HR", book, fallback=fallback)


if __name__ == "__main__":
    import sys
    _book = sys.argv[1] if len(sys.argv) > 1 else PREFERRED_BOOK
    board = fetch_hit_prop_odds(book=None if _book == "all" else _book)
    for k, v in sorted(board.items(), key=lambda kv: kv[1]["implied"] or 0, reverse=True):
        o = v["odds"]
        print(f"  {v['player']:24} {v['book']:11} {'+' if o > 0 else ''}{o:<5} "
              f"implied {v['implied']}")
