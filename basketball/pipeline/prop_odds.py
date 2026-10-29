"""
Player-prop odds for NBA. Twin of baseball/pipeline/prop_odds.py (throttle, retry,
pagination, name matching): a logic fix in one needs checking in the other. NBA lines are
real values (Over 24.5), read from the book rather than fixed at 0.5.
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
    and 'P.J. Tucker' == 'PJ Tucker' — for matching book names to our rosters."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


# A single book, so a parlay is placeable.
DEFAULT_BOOK = "fanduel"


# ── NBA markets ──────────────────────────────────────────────────────────────
# Row shape:
#     player_name    "Shai Gilgeous-Alexander"   <- no player id
#     market_type    "player_assists"
#     stat_category  "assists"
#     line           3.5
#     selection_type "over" | "under"
#     odds_american  -700
#     is_main_line   true | false                 <- false means an ALTERNATE line

MARKET_POINTS   = "player_points"
MARKET_REBOUNDS = "player_rebounds"
MARKET_ASSISTS  = "player_assists"
MARKET_THREES   = "player_made_threes"
NBA_MARKETS = (MARKET_POINTS, MARKET_REBOUNDS, MARKET_ASSISTS, MARKET_THREES)


def fetch_prop_odds(market: str, book: str | None = DEFAULT_BOOK,
                    main_line_only: bool = True) -> dict:
    """Player props for one market, keyed by normalized player name.

    Returns {name: {"line": 24.5, "over": -115, "under": -105, "book": "fanduel",
                    "event_id": ..., "start": ...}}. Over and under arrive as separate
    rows and are paired here; `main_line_only` skips alternate lines.
    """
    rows = _board_rows(market)
    if rows is None:
        return {}
    return _parse_main(rows, market, book, main_line_only)


def _board_rows(market: str) -> list | None:
    """Every row on the board for one market — main AND alternate lines, both books.
    One paginated fetch; callers parse what they need from it."""
    if not PROP_ODDS_API_KEY or not _BASE:
        print(f"[prop_odds] PROP_ODDS_API_KEY / PROP_ODDS_BASE_URL not set — skipping {market}")
        return None
    try:
        return _paginate("odds", {"league": "nba", "market": market})
    except Exception as e:
        print(f"[prop_odds] {market} fetch failed: {e}")
        return None


def _pregame(r: dict) -> bool:
    """A current pregame price: the board also carries live, inactive and stale rows."""
    return (not r.get("is_live") and r.get("is_active") is not False
            and not r.get("is_stale_pregame_price"))


def _parse_main(rows: list, market: str, book: str | None, main_line_only: bool) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        if not r.get("is_player_prop") or r.get("market_type") != market:
            continue
        if main_line_only and not r.get("is_main_line"):
            continue
        if not _pregame(r):
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
        # The book moved the line: keep the latest and reset its prices.
        if entry["line"] != line:
            entry.update({"line": line, "over": None, "under": None})
        side = r.get("selection_type")
        if side in ("over", "under"):
            entry[side] = r.get("odds_american")

    print(f"[prop_odds] {len(out)} players with {market} lines"
          f"{' [' + book + ']' if book else ''} ({len(rows)} rows scanned)")
    return out


def _decimal(odds: int) -> float:
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / abs(odds))


def _parse_ladders(rows: list, market: str, book: str | None) -> tuple[dict, int]:
    """Alternate OVER lines below each player's main line, per game.

    Returns ({name: [ladder, ...]}, rungs_dropped). A ladder is
    {player_name, event_id, start, main_line, main_over, rungs: [(line, odds), ...]} with
    rungs nearest-first; a name has one per game. A rung priced longer than the one above
    it is dropped (a lower line can't be less likely) and counted.
    """
    groups: dict[tuple, list] = {}
    for r in rows:
        if not r.get("is_player_prop") or r.get("market_type") != market:
            continue
        if book and r.get("sportsbook") != book:
            continue
        if r.get("selection_type") != "over" or not _pregame(r):
            continue
        if not r.get("player_name") or r.get("line") is None or r.get("odds_american") is None:
            continue
        groups.setdefault((normalize_name(r["player_name"]), r.get("event_id")), []).append(r)

    out: dict[str, list] = {}
    dropped = 0
    for (key, event_id), rs in groups.items():
        main = next((r for r in rs if r.get("is_main_line")), None)
        if main is None:
            continue                  # nothing to undercut FROM
        m_line, m_odds = main["line"], main["odds_american"]
        below = sorted({(r["line"], r["odds_american"]) for r in rs if r["line"] < m_line},
                       key=lambda x: (-x[0], _decimal(x[1])))
        ceiling, kept = _decimal(m_odds), []
        for line, odds in below:
            if _decimal(odds) <= ceiling + 1e-9:
                kept.append((line, odds))
                ceiling = _decimal(odds)
            else:
                dropped += 1
        out.setdefault(key, []).append({
            "player_name": main["player_name"], "event_id": event_id,
            "start": main.get("event_start_time"), "main_line": m_line,
            "main_over": m_odds, "rungs": kept,
        })
    return out, dropped


def fetch_prop_board(market: str, book: str | None = DEFAULT_BOOK) -> tuple[dict, dict]:
    """Main lines and the alternate ladder for one market, from one paginated fetch."""
    rows = _board_rows(market)
    if rows is None:
        return {}, {}
    main = _parse_main(rows, market, book, True)
    ladders, dropped = _parse_ladders(rows, market, book)
    rungs = sum(len(l["rungs"]) for ls in ladders.values() for l in ls)
    print(f"[prop_odds] {market} ladders: {rungs} alternate rungs across {len(ladders)} "
          f"players; {dropped} dropped as priced backwards")
    return main, ladders
