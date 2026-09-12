"""
Player-prop odds for NBA — copied from the baseball module 2026-09-12.

SHARED ORIGIN: `baseball/pipeline/prop_odds.py`. The throttle / retry / pagination /
name-normalisation half is a verbatim copy; if a genuine logic bug is fixed in one, check
the other. See PROJECT.md §25.

STRUCTURAL DIFFERENCE from baseball: MLB hit and HR props are binary ("Over 0.5"), so the
baseball module hardcodes that line. NBA props are real lines — Over 24.5 points, Over 8.5
rebounds — so the value itself must be read from the book and carried through. The
market-specific half was NOT copied; see the TODO at the bottom.
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
# The API caps a page at 200 rows (larger limits are silently clamped). Requesting the
# max matters a lot here: every extra page costs a full throttle interval, so 50/page
# turned one board into ~11 requests (~57s) where 200/page needs ~3 (~16s).
_PAGE_SIZE    = 200
_MAX_PAGES    = 20          # safety cap; a full NBA slate of props is well under this
_TIMEOUT      = 20
_last_call    = 0.0

# Transient failures worth retrying — gateway/upstream blips (a single 502 on page 0
# would otherwise kill the whole fetch) and rate-limit/timeout hiccups.
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
    """Walk the paginated odds feed via offset until has_more is false.

    A mid-walk failure keeps the rows already collected instead of discarding
    them — the API 400s past a deep offset (~free-tier cap) even when
    `has_more` said otherwise, and 550 good rows beat zero. Only a first-page
    failure propagates (nothing fetched ⇒ caller reports the fetch as failed)."""
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
        # `next_offset` can come back explicitly null even with has_more=true, and
        # dict.get()'s default only covers a *missing* key — taking it verbatim put
        # None into offset and blew up the next iteration on None + _PAGE_SIZE,
        # discarding every row already fetched. Fall back to a computed offset.
        next_offset = pg.get("next_offset")
        offset = next_offset if isinstance(next_offset, int) else offset + _PAGE_SIZE
    return rows


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation so 'Julio Rodríguez' == 'Julio Rodriguez'
    and 'P.J. Tucker' == 'PJ Tucker' — for matching book names to our rosters."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


# Restrict to a single book so a 2-leg parlay is actually placeable (you can't
# parlay a DraftKings leg with a FanDuel leg). Default FanDuel — the only book the
# user bets. Pass book="draftkings" or book=None (best-of-all) to widen coverage.
DEFAULT_BOOK = "fanduel"


# ── NBA markets ──────────────────────────────────────────────────────────────
# Verified against the live feed 2026-09-12. Row shape (differs from baseball):
#     player_name    "Shai Gilgeous-Alexander"   <- NOT `player` / `player_id`, both absent
#     market_type    "player_assists"
#     stat_category  "assists"
#     line           3.5                          <- baseball's Over-0.5 is implicit; here it is data
#     selection_type "over" | "under"
#     odds_american  -700
#     is_main_line   true | false                 <- false means an ALTERNATE line
#
# Markets confirmed present: player_points, player_made_threes, player_assists,
# player_rebounds, the +combos, player_double_double, player_triple_double, player_blocks.

MARKET_POINTS   = "player_points"
MARKET_REBOUNDS = "player_rebounds"
MARKET_ASSISTS  = "player_assists"
MARKET_THREES   = "player_made_threes"
NBA_MARKETS = (MARKET_POINTS, MARKET_REBOUNDS, MARKET_ASSISTS, MARKET_THREES)


def fetch_prop_odds(market: str, book: str | None = DEFAULT_BOOK,
                    main_line_only: bool = True) -> dict:
    """Player props for one market, keyed by normalized player name.

    Returns {name: {"line": 24.5, "over": -115, "under": -105, "book": "fanduel",
                    "event_id": ..., "start": ...}}

    `main_line_only` defaults True: the feed carries alternate lines
    (`is_main_line: false`) alongside the primary one, and mixing them would put several
    different lines on the same player with no way to tell which the book is actually
    featuring.

    Over and Under arrive as SEPARATE rows for the same player+line, so they are paired
    here — a caller wanting to price both sides needs them together.
    """
    if not PROP_ODDS_API_KEY or not _BASE:
        print(f"[prop_odds] PROP_ODDS_API_KEY / PROP_ODDS_BASE_URL not set — skipping {market}")
        return {}
    try:
        rows = _paginate("odds", {"league": "nba", "market": market})
    except Exception as e:
        print(f"[prop_odds] {market} fetch failed: {e}")
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        if not r.get("is_player_prop") or r.get("market_type") != market:
            continue
        if main_line_only and not r.get("is_main_line"):
            continue
        if book and r.get("sportsbook") != book:
            continue
        name = r.get("player_name")
        line = r.get("line")
        if not name or line is None:
            continue
        key = normalize_name(name)
        entry = out.setdefault(key, {
            "player_name": name, "line": line, "over": None, "under": None,
            "book": r.get("sportsbook"), "event_id": r.get("event_id"),
            "start": r.get("event_start_time"), "market": market,
        })
        # A different main line for the same player means the book moved it; keep the
        # most recent and reset the paired prices rather than mixing two lines.
        if entry["line"] != line:
            entry.update({"line": line, "over": None, "under": None})
        side = r.get("selection_type")
        if side in ("over", "under"):
            entry[side] = r.get("odds_american")

    print(f"[prop_odds] {len(out)} players with {market} lines"
          f"{' [' + book + ']' if book else ''} ({len(rows)} rows scanned)")
    return out
