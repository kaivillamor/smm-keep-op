"""Always-on dashboard + collection scheduler, one process because a Railway volume
attaches to one service only. Research data only: bets.db is never deployed."""
import math
import os
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import subprocess
import sys

# baseball/'s default paths are relative to it, so keep the directory as well.
BASEBALL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "baseball"))
sys.path.insert(0, BASEBALL_DIR)
# Never on sys.path: basketball/ runs as a subprocess and its DB is read with sqlite3.
BASKETBALL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "basketball"))

# uvicorn puts the repo root on sys.path, not web/, so add web/ for `from users import`.
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WEB_DIR)

@asynccontextmanager
async def lifespan(_: "FastAPI"):
    try:
        seeded = bootstrap_from_env()
        if seeded:
            print(f"[users] seeded {seeded} account(s) from environment", flush=True)
    except Exception as e:
        print(f"[users] bootstrap failed: {type(e).__name__}: {e}", flush=True)
    # Run research.py's migrations before serving: _db() reads the table directly.
    try:
        from output.research import _connect as _research_connect
        # The resolved path: research.py's default is relative to the CWD.
        _research_connect(_research_db_path()).close()
        print("[research] schema ensured", flush=True)
    except Exception as e:
        print(f"[research] schema ensure failed: {type(e).__name__}: {e}", flush=True)
    # The NBA DB is read-only here, so its collector migrates it.
    try:
        if _run_basketball("--migrate") == 0:
            print("[nba] schema ensured", flush=True)
    except Exception as e:
        print(f"[nba] schema ensure failed: {type(e).__name__}: {e}", flush=True)
    threading.Thread(target=_scheduler, daemon=True).start()
    print("[scheduler] started", flush=True)
    yield


app = FastAPI(title="MLB Model Research", lifespan=lifespan)

# Users live in users.db (web/users.py); DASH_USERS / DASH_PASS only seed it.
from users import authenticate, bootstrap_from_env   # noqa: E402  (needs sys.path above)

SESSION_COOKIE = "mlbsession"
SESSION_MAX_AGE = 60 * 60 * 24 * 14        # 14 days

_secret_env = os.getenv("SESSION_SECRET")
if not _secret_env:
    print("[auth] SESSION_SECRET unset — generating an ephemeral one. Every restart will "
          "sign everyone out. Set SESSION_SECRET in the environment.", flush=True)
SESSION_SECRET = (_secret_env or secrets.token_hex(32)).encode()


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session(username: str) -> str:
    """Stateless signed token: <username>|<expiry>|<signature>."""
    payload = f"{username}|{int(time.time()) + SESSION_MAX_AGE}"
    return f"{payload}|{_sign(payload)}"


def read_session(token: str | None) -> str | None:
    """Returns the username, or None if absent/expired/tampered."""
    if not token:
        return None
    try:
        username, expiry, sig = token.rsplit("|", 2)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(f"{username}|{expiry}")):
        return None
    if int(expiry) < time.time():
        return None
    return username


def current_user(request: Request) -> tuple[str, str] | None:
    """(username, role) for a valid session, else None; never raises."""
    from users import get_user
    name = read_session(request.cookies.get(SESSION_COOKIE))
    if not name:
        return None
    user = get_user(name)
    return (user["username"], user["role"]) if user else None


def auth(request: Request) -> tuple[str, str]:
    who = current_user(request)
    if not who:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return who


def require_admin(who: tuple[str, str] = Depends(auth)) -> tuple[str, str]:
    """Admin-only gate, applied to the data endpoints, not just the pages."""
    if who[1] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return who


def _research_db_path() -> str:
    """Absolute path to research.db: a relative RESEARCH_DB is anchored to BASEBALL_DIR,
    not the CWD."""
    from output.research import RESEARCH_DB
    return RESEARCH_DB if os.path.isabs(RESEARCH_DB) else os.path.join(BASEBALL_DIR, RESEARCH_DB)


def _db():
    conn = sqlite3.connect(_research_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _auc(scores: list[float], ys: list[int]) -> tuple[float, float, float]:
    """(AUC, CI low, CI high), Hanley–McNeil. Twin of basketball/output/research._auc,
    which can't be imported here: change both."""
    order = sorted(range(len(scores)), key=scores.__getitem__)
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in order[i:j + 1]:
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    n1 = sum(ys)
    n0 = len(ys) - n1
    if not n1 or not n0:
        return math.nan, math.nan, math.nan
    a = (sum(r for r, y in zip(ranks, ys) if y) - n1 * (n1 + 1) / 2) / (n1 * n0)
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    se = math.sqrt((a * (1 - a) + (n1 - 1) * (q1 - a * a) + (n0 - 1) * (q2 - a * a))
                   / (n1 * n0))
    return a, a - 1.96 * se, a + 1.96 * se


def stats(db=_db, where: str = "", bench: bool = False) -> dict:
    """Calibration + ranking signal for either sport. `db` is the connection factory;
    `where` is an SQL condition written in code, never request input; `bench` adds the
    model-vs-price AUCs (NBA only)."""
    only = f" WHERE {where}" if where else ""
    try:
        c = db()
        rows = [dict(r) for r in c.execute(
            "SELECT model_prob p, outcome, date, book_implied bi FROM predictions "
            # NBA logs rows with no probability, which would crash the sums below.
            "WHERE outcome IN ('win','loss') AND model_prob IS NOT NULL"
            + (f" AND ({where})" if where else ""))]
        total = c.execute(f"SELECT COUNT(*) FROM predictions{only}").fetchone()[0]
        by_day = [dict(r) for r in c.execute(
            "SELECT date, COUNT(*) n, SUM(outcome IN ('win','loss')) graded "
            f"FROM predictions{only} GROUP BY date ORDER BY date DESC LIMIT 10")]
        c.close()
    except Exception as e:
        return {"error": str(e), "graded": 0, "logged": 0, "by_day": []}

    n = len(rows)
    out = {"logged": total, "graded": n, "by_day": by_day, "error": None}
    if not n:
        return out

    hit = lambda g: sum(1 for r in g if r["outcome"] == "win") / len(g)
    out["predicted"] = sum(r["p"] for r in rows) / n
    out["actual"] = hit(rows)
    out["overconfidence"] = out["predicted"] - out["actual"]
    # What the book's prices implied on the same rows: the hit rate a bet needed.
    priced = [r["bi"] for r in rows if r["bi"] is not None]
    out["priced"] = sum(priced) / len(priced) if priced else None
    # Ranking skill against the book's own price, on the same rows.
    pr = [r for r in rows if r["bi"] is not None]
    if bench and len(pr) >= 200:
        ys = [1 if r["outcome"] == "win" else 0 for r in pr]
        out["auc_n"] = len(pr)
        out["auc"], out["auc_lo"], out["auc_hi"] = _auc([r["p"] for r in pr], ys)
        out["auc_book"], out["auc_book_lo"], out["auc_book_hi"] = _auc(
            [r["bi"] for r in pr], ys)

    rows.sort(key=lambda r: r["p"])
    half = n // 2
    if half:
        lo, hi = hit(rows[:half]), hit(rows[half:])
        out["lower"], out["upper"], out["gap"] = lo, hi, hi - lo
        se = math.sqrt(lo * (1 - lo) / half + hi * (1 - hi) / (n - half))
        out["gap_ci_low"] = (hi - lo) - 1.96 * se
        out["gap_ci_high"] = (hi - lo) + 1.96 * se

    buckets = []
    for lo_b, hi_b in ((0, .55), (.55, .65), (.65, .72), (.72, .80), (.80, 1.01)):
        g = [r for r in rows if lo_b <= r["p"] < hi_b]
        if g:
            buckets.append({"range": f"{lo_b:.0%}-{hi_b:.0%}", "n": len(g),
                            "predicted": sum(r["p"] for r in g) / len(g),
                            "actual": hit(g)})
    out["buckets"] = buckets
    return out


def _mlb_stats() -> dict:
    """MLB calibration on the evidence rows (research.EVIDENCE_SQL, as `research.py
    report` uses)."""
    from output.research import EVIDENCE_SQL
    return stats(where=EVIDENCE_SQL)


@app.get("/healthz")
def healthz():
    """Unauthenticated so Railway can health-check without credentials."""
    return {"ok": True, "utc": datetime.now(timezone.utc).isoformat()}


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(body: LoginBody, request: Request, response: Response):
    user = authenticate(body.username, body.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    response.set_cookie(
        SESSION_COOKIE, make_session(user["username"]),
        max_age=SESSION_MAX_AGE,
        httponly=True,                                  # JS cannot read it -> XSS can't steal it
        secure=request.url.scheme == "https",           # HTTPS-only in production, still works on localhost
        samesite="lax",                                 # not sent on cross-site POSTs -> basic CSRF defence
        path="/")
    return {"user": user["username"], "role": user["role"]}


@app.post("/api/logout")
def api_logout(response: Response):
    """Sign out: clearing the cookie ends the session."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    """The signed-in user and role, or nulls when anonymous (not a 401)."""
    who = current_user(request)
    return {"user": who[0], "role": who[1]} if who else {"user": None, "role": None}


@app.get("/api/summary")
def api_summary(who: tuple = Depends(auth)):
    """Headline numbers for any signed-in user; buckets, gap and slope are admin-only
    (/api/report)."""
    s = _mlb_stats()
    return {"logged": s.get("logged", 0), "graded": s.get("graded", 0),
            "actual": s.get("actual"), "predicted": s.get("predicted"),
            "role": who[1], "user": who[0], "error": s.get("error")}


@app.get("/api/slate")
def api_slate(_: tuple = Depends(auth), limit: int = 200):
    """Today's ungraded predictions ranked by edge: raw model_prob, the stored
    calibrated_prob (never recomputed), and the per-leg EV gate's verdict (`blocked_by`
    code, `blocked_detail` numbers)."""
    from model.factors.hit_model import (leg_edge, leg_gate_reason,
                                         MIN_EDGE_PTS, EV_GATE_LIVE)
    try:
        c = _db()
        day = c.execute(
            "SELECT MAX(date) FROM predictions WHERE outcome IS NULL").fetchone()[0]
        rows = [dict(r) for r in c.execute(
            "SELECT batter_name, team, pitcher_name, lineup_pos, model_prob, "
            "       calibrated_prob, book_odds, book_implied, season_phase, date "
            "FROM predictions WHERE outcome IS NULL AND date = ? "
            "ORDER BY model_prob DESC LIMIT ?", (day, min(limit, 400)))] if day else []
        c.close()
    except Exception as e:
        return {"error": str(e), "date": None, "legs": []}

    legs = []
    for r in rows:
        cal = r["calibrated_prob"]
        # The pipeline's own gate, so the dashboard can't disagree with what surfaces.
        why = leg_gate_reason(cal, r["book_implied"], r["season_phase"])
        legs.append({
            "name": r["batter_name"], "team": r["team"], "pitcher": r["pitcher_name"],
            "spot": r["lineup_pos"], "model_prob": r["model_prob"], "calibrated": cal,
            "odds": r["book_odds"], "book_implied": r["book_implied"],
            "edge": leg_edge(cal, r["book_implied"]),
            "clears_gate": why is None,
            "blocked_by": None if why is None else why[0],
            "blocked_detail": None if why is None else why[1],
        })
    legs.sort(key=lambda l: l["edge"] if l["edge"] is not None else float("-inf"),
              reverse=True)
    return {"error": None, "date": day, "count": len(legs), "legs": legs,
            "gate": {"kind": "per_leg_ev", "min_edge": MIN_EDGE_PTS,
                     "live": EV_GATE_LIVE,
                     "clearing": sum(1 for l in legs if l["clears_gate"])}}


@app.get("/api/hits")
def api_hits(request: Request, limit: int = 60):
    """Recent winning research legs (never bets) for the ambient feed."""
    try:
        c = _db()
        rows = [dict(r) for r in c.execute(
            "SELECT batter_name, team, actual_hits, model_prob, date "
            "FROM predictions WHERE outcome='win' AND batter_name IS NOT NULL "
            "ORDER BY date DESC, model_prob DESC LIMIT ?", (min(limit, 200),))]
        c.close()
    except Exception as e:
        return {"error": str(e), "hits": []}
    # Public (the login screen's background): the model's probability only when signed in.
    signed_in = current_user(request) is not None
    return {"error": None, "hits": [
        {"name": r["batter_name"], "team": r["team"], "hits": r["actual_hits"],
         "date": r["date"], **({"prob": r["model_prob"]} if signed_in else {})}
        for r in rows]}


@app.get("/admin", response_class=HTMLResponse)
def admin():
    """The SPA, served here so a client-side route doesn't 404."""
    return index()


@app.get("/nba", response_class=HTMLResponse)
@app.get("/nba/admin", response_class=HTMLResponse)
def nba_pages():
    """Same SPA, for the same reason as /admin."""
    return index()


@app.get("/api/report")
def api_report(_: tuple = Depends(require_admin)):
    return _mlb_stats()


# ── NBA research views ───────────────────────────────────────────────────────
# Read with plain sqlite3, never by importing basketball/; restated values name their twin.

# Twins of untrusted() and the consistency settings in basketball/parlay/method.py.
NBA_TRUSTED_SQL = ("(minutes_avg >= 28 AND market IN ('player_points', 'player_rebounds', "
                   "'player_assists')) OR (minutes_avg >= 20 AND market IN ('player_points', "
                   "'player_assists'))")
NBA_TRUSTED_RULE = "28+ minutes in points, rebounds or assists, or 20+ in points or assists"
NBA_CONSISTENT_SQL = "hit_l10 >= 0.7 AND hit_l20 >= 0.6"

# The method parlays' money count: None counts nothing; a slate date counts from that date.
NBA_RECORD_FROM: str | None = None

def _nba_db_path() -> str:
    """Absolute NBA research DB path, anchored to BASKETBALL_DIR (the collector's cwd).
    Twin of RESEARCH_DB in basketball/output/research.py."""
    p = os.getenv("NBA_RESEARCH_DB_PATH") or "data/history/research.db"
    return p if os.path.isabs(p) else os.path.join(BASKETBALL_DIR, p)


def _nba_db():
    """Read-only: the collector owns this file. as_uri() because the local path has a
    space."""
    conn = sqlite3.connect(f"{Path(_nba_db_path()).as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _nba_stale_cutoff() -> str:
    """Slate date before which every prediction should be graded: yesterday, Eastern
    (fixed UTC-5, twin of slate_date() in basketball/pipeline/calendar.py)."""
    today = datetime.now(timezone.utc) - timedelta(hours=5)
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")


@app.get("/api/nba/summary")
def api_nba_summary(who: tuple = Depends(auth)):
    """Headline NBA numbers and grading backlog for any signed-in user; the rest is
    admin-only (/api/nba/report)."""
    s = stats(_nba_db, bench=True)
    out = {"logged": s.get("logged", 0), "graded": s.get("graded", 0),
           "actual": s.get("actual"), "predicted": s.get("predicted"),
           **{k: s.get(k) for k in ("auc", "auc_lo", "auc_hi", "auc_book", "auc_book_lo",
                                    "auc_book_hi", "auc_n")},
           "role": who[1], "user": who[0], "error": s.get("error"),
           "slates": [], "stale_pending": 0, "stale_since": None}
    if out["error"]:
        return out
    try:
        c = _nba_db()
        out["slates"] = [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM predictions ORDER BY date DESC")]
        out["stale_pending"], out["stale_since"] = c.execute(
            "SELECT COUNT(*), MIN(date) FROM predictions WHERE outcome IS NULL AND date < ?",
            (_nba_stale_cutoff(),)).fetchone()
        c.close()
    except Exception as e:
        out["error"] = str(e)
    return out


@app.get("/api/nba/slate")
def api_nba_slate(_: tuple = Depends(auth),
                  day: str | None = Query(None, alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """Every prop logged for one slate (default: the latest), ranked by model probability
    with players listed out last. No edge column: NBA has no fitted calibration."""
    try:
        c = _nba_db()
        day = day or c.execute("SELECT MAX(date) FROM predictions").fetchone()[0]
        rows = [dict(r) for r in c.execute(
            "SELECT player_name, team, opponent, market, line, book_odds, book_implied, "
            "       base_prob, owner_adj, model_prob, hit_l10, injury_status, outcome, "
            "       actual_value, banned "
            "FROM predictions WHERE date = ? "
            "ORDER BY COALESCE(injury_status = 'out', 0), model_prob IS NULL, "
            "         model_prob DESC LIMIT 800", (day,))] if day else []
        c.close()
    except Exception as e:
        return {"error": str(e), "date": None, "props": []}
    return {"error": None, "date": day, "count": len(rows), "props": [
        {"name": r["player_name"], "team": r["team"], "opp": r["opponent"],
         "market": r["market"], "line": r["line"], "odds": r["book_odds"],
         "implied": r["book_implied"], "quant": r["base_prob"], "rules": r["owner_adj"],
         "model": r["model_prob"], "l10": r["hit_l10"], "injury": r["injury_status"],
         "outcome": r["outcome"], "actual": r["actual_value"], "banned": r["banned"]}
        for r in rows]}


@app.get("/api/nba/parlays")
def api_nba_parlays(_: tuple = Depends(auth), limit: int = 40):
    """The method's paper parlays, newest first, with their legs and a per-tier record.
    `staked` counts settled win/loss parlays only; `record` is empty while NBA_RECORD_FROM
    is None."""
    try:
        c = _nba_db()
        parlays = [dict(r) for r in c.execute(
            "SELECT id, date, tier, stake, n_legs, american_odds, profit_if_win, first_tip, "
            "       outcome, profit "
            "FROM method_parlays ORDER BY date DESC, stake LIMIT ?", (min(limit, 200),))]
        legs: dict[int, list] = {}
        ids = [p["id"] for p in parlays]
        for r in (c.execute(
                "SELECT parlay_id, player_name, team, opponent, market, line, main_line, "
                "       undercut, odds, hit_l10, hit_l20, h2h_hits, h2h_meetings, sim_hits, "
                "       sim_games, position, opp_pos_rank, outcome, actual_value "
                f"FROM method_legs WHERE parlay_id IN ({','.join('?' * len(ids))}) "
                "ORDER BY id", ids) if ids else []):
            legs.setdefault(r["parlay_id"], []).append({
                "name": r["player_name"], "team": r["team"], "opp": r["opponent"],
                "market": r["market"], "line": r["line"], "main_line": r["main_line"],
                "undercut": r["undercut"], "odds": r["odds"], "l10": r["hit_l10"],
                "l20": r["hit_l20"], "h2h_hits": r["h2h_hits"],
                "h2h_games": r["h2h_meetings"], "sim_hits": r["sim_hits"],
                "sim_games": r["sim_games"], "position": r["position"],
                "pos_rank": r["opp_pos_rank"], "outcome": r["outcome"],
                "actual": r["actual_value"]})
        record = [] if NBA_RECORD_FROM is None else [dict(r) for r in c.execute(
            "SELECT tier, SUM(outcome='win') won, SUM(outcome='loss') lost, "
            "       SUM(outcome='void') void, "
            "       SUM(CASE WHEN outcome IN ('win','loss') THEN stake ELSE 0 END) staked, "
            "       SUM(COALESCE(profit, 0)) profit "
            "FROM method_parlays WHERE outcome IS NOT NULL AND date >= ? "
            "GROUP BY tier ORDER BY MIN(stake)", (NBA_RECORD_FROM,))]
        c.close()
    except Exception as e:
        return {"error": str(e), "record": [], "record_from": NBA_RECORD_FROM, "parlays": []}
    return {"error": None, "record": record, "record_from": NBA_RECORD_FROM, "parlays": [
        {**{k: p[k] for k in ("date", "tier", "stake", "n_legs", "american_odds",
                               "profit_if_win", "first_tip", "outcome", "profit")},
         "legs": legs.get(p["id"], [])}
        for p in parlays]}


@app.get("/api/nba/report")
def api_nba_report(_: tuple = Depends(require_admin)):
    """NBA calibration (the shared stats()) plus `trusted` and `pools` (the method's
    player pools), `by_market`, and `health` (collection and grading per slate)."""
    out = stats(_nba_db)
    if out.get("error"):
        return out
    out["trusted"] = {**stats(_nba_db, NBA_TRUSTED_SQL), "label": "Trusted",
                      "rule": NBA_TRUSTED_RULE}
    keep = ("label", "graded", "predicted", "actual", "priced", "gap", "gap_ci_low",
            "gap_ci_high")
    out["pools"] = [{k: s.get(k) for k in keep} for s in (
        {**stats(_nba_db, f"({NBA_TRUSTED_SQL}) AND {NBA_CONSISTENT_SQL}"),
         "label": "Trusted, 7/10, 12/20"},
        out["trusted"],
        {**out, "label": "Everyone"})]
    try:
        c = _nba_db()
        out["by_market"] = [dict(r) for r in c.execute(
            "SELECT market, COUNT(*) n, AVG(model_prob) predicted, AVG(outcome='win') actual "
            "FROM predictions WHERE outcome IN ('win','loss') AND model_prob IS NOT NULL "
            "GROUP BY market ORDER BY n DESC")]
        cutoff = _nba_stale_cutoff()
        out["health"] = [dict(r, stale=bool(r["pending"]) and r["date"] < cutoff)
                         for r in c.execute(
            "SELECT date, COUNT(*) logged, SUM(model_prob IS NOT NULL) scored, "
            "       SUM(book_odds IS NOT NULL) priced, "
            # COALESCE: the SUM over a slate with nothing graded is NULL, not 0.
            "       COALESCE(SUM(outcome IN ('win','loss')), 0) graded, "
            "       COALESCE(SUM(outcome = 'void'), 0) void, "
            "       SUM(outcome IS NULL) pending "
            "FROM predictions GROUP BY date ORDER BY date DESC LIMIT 30")]
        c.close()
    except Exception as e:
        out["error"] = str(e)
    return out


# ── NBA bans ─────────────────────────────────────────────────────────────────
# The dashboard writes nba_bans.db; the collector reads it at the start of each run.

def _nba_bans_path() -> str:
    """nba_bans.db, beside the NBA research DB. Twin of BANS_DB in
    basketball/output/research.py."""
    return os.path.join(os.path.dirname(_nba_db_path()), "nba_bans.db")


def _nba_bans_db():
    p = _nba_bans_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            kind      TEXT NOT NULL,     -- 'player' | 'team'
            espn_id   TEXT NOT NULL,
            name      TEXT NOT NULL,     -- player name, or team abbreviation
            team      TEXT,              -- a player's team when banned
            why       TEXT,
            added_by  TEXT,
            added_at  TEXT NOT NULL,
            PRIMARY KEY (kind, espn_id)
        )""")
    conn.commit()
    return conn


def _nba_ban_list(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT kind, espn_id, name, team, why, added_by, added_at FROM bans "
        "ORDER BY added_at DESC")]


class BanBody(BaseModel):
    kind: Literal["player", "team"]
    espn_id: str = Field(pattern=r"^\d{1,12}$")
    why: str = Field("", max_length=200)


@app.get("/api/nba/bans")
def api_nba_bans(_: tuple = Depends(require_admin)):
    """The method's ban list, newest first, and the picker's choices: every player with a
    logged prop (latest name and team) and every team."""
    c = _nba_bans_db()
    bans = _nba_ban_list(c)
    c.close()
    try:
        n = _nba_db()
        # With one MAX(), SQLite takes the bare columns from that row: the latest name and team.
        players = [dict(r) for r in n.execute(
            "SELECT player_id espn_id, player_name name, team, MAX(date) last_seen "
            "FROM predictions GROUP BY player_id ORDER BY player_name")]
        teams = [dict(r) for r in n.execute(
            "SELECT DISTINCT team_id espn_id, team name FROM team_games ORDER BY team")]
        n.close()
    except Exception as e:
        return {"error": str(e), "bans": bans, "players": [], "teams": []}
    return {"error": None, "bans": bans, "players": players, "teams": teams}


@app.post("/api/nba/bans")
def api_nba_ban(body: BanBody, who: tuple = Depends(require_admin)):
    """Ban a player or team by ESPN id. The name comes from the NBA DB, so an id it has
    never logged is refused."""
    try:
        n = _nba_db()
        if body.kind == "player":
            row = n.execute("SELECT player_name, team FROM predictions WHERE player_id = ? "
                            "ORDER BY date DESC LIMIT 1", (body.espn_id,)).fetchone()
        else:
            row = n.execute("SELECT team, NULL FROM team_games WHERE team_id = ? LIMIT 1",
                            (body.espn_id,)).fetchone()
        n.close()
    except Exception as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"NBA DB unavailable: {e}")
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No NBA {body.kind} with ESPN id {body.espn_id}")
    c = _nba_bans_db()
    c.execute("INSERT OR IGNORE INTO bans (kind, espn_id, name, team, why, added_by, added_at) "
              "VALUES (?,?,?,?,?,?,?)",
              (body.kind, body.espn_id, row[0], row[1], body.why.strip() or None, who[0],
               datetime.now(timezone.utc).isoformat()))
    c.commit()
    bans = _nba_ban_list(c)
    c.close()
    return {"bans": bans}


@app.delete("/api/nba/bans/{kind}/{espn_id}")
def api_nba_unban(kind: Literal["player", "team"], espn_id: str,
                  _: tuple = Depends(require_admin)):
    """Lift a ban; returns the list left."""
    c = _nba_bans_db()
    c.execute("DELETE FROM bans WHERE kind = ? AND espn_id = ?", (kind, espn_id))
    c.commit()
    bans = _nba_ban_list(c)
    c.close()
    return {"bans": bans}


FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")

# Assets need no auth: compiled JS/CSS, no data. Data endpoints require a session.
if os.path.isdir(os.path.join(FRONTEND_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")),
              name="assets")


@app.get("/", response_class=HTMLResponse)
def index():
    """Serves the built React app."""
    idx = os.path.join(FRONTEND_DIST, "index.html")
    if not os.path.exists(idx):
        return HTMLResponse(
            "<h1>Front-end not built</h1><p>Run <code>npm ci && npm run build</code> in "
            "<code>web/frontend</code>, or deploy via the Dockerfile which does it.</p>",
            status_code=501)
    return FileResponse(idx)


# ── background collection ────────────────────────────────────────────────────
COLLECT_HOURS = {int(h) for h in
                 (os.getenv("COLLECT_HOURS") or "19,20,21,22,23,0,1,2").split(",")}
GRADE_HOUR = int(os.getenv("GRADE_HOUR") or 13)

NBA_COLLECT_HOURS = {int(h) for h in
                     (os.getenv("NBA_COLLECT_HOURS") or "21,22,23,0,1").split(",")}
NBA_GRADE_HOUR = int(os.getenv("NBA_GRADE_HOUR") or 11)
NBA_ENABLED = (os.getenv("NBA_ENABLED") or "1") not in ("0", "false", "False")
# MLB's pause switch: an empty COLLECT_HOURS falls back to the default hours.
MLB_ENABLED = (os.getenv("MLB_ENABLED") or "1") not in ("0", "false", "False")


def _run_basketball(arg: str) -> int:
    """Run the NBA collector as a subprocess. Never import basketball/ here: its module
    names collide with baseball's, which is already on sys.path."""
    proc = subprocess.run([sys.executable, "main.py", arg], cwd=BASKETBALL_DIR,
                          capture_output=True, text=True, timeout=1800)
    for line in (proc.stdout or "").splitlines()[-25:]:
        print(f"[nba] {line}", flush=True)
    if proc.returncode != 0:
        print(f"[nba] {arg} exited {proc.returncode}: "
              f"{(proc.stderr or '').strip()[-400:]}", flush=True)
    return proc.returncode


def _scheduler():
    """Hourly collection and daily grading for both sports. Collection is idempotent, so a
    repeated pass is harmless."""
    done: set[str] = set()
    while True:
        try:
            now = datetime.now(timezone.utc)
            key = f"{now:%Y-%m-%d}-{now.hour}"
            if MLB_ENABLED and key not in done:
                # Its own try, so an MLB failure can't skip NBA; unmarked, so it retries.
                try:
                    if now.hour == GRADE_HOUR:
                        from output.research import grade_predictions
                        grade_predictions()
                        done.add(key)
                    elif now.hour in COLLECT_HOURS:
                        from main import _collect_predictions
                        _collect_predictions()
                        done.add(key)
                except Exception as e:
                    print(f"[scheduler] MLB run failed: {type(e).__name__}: {e}", flush=True)

            # Keyed apart from MLB: their hours overlap.
            nba_key = f"nba-{key}"
            if NBA_ENABLED and nba_key not in done:
                if now.hour == NBA_GRADE_HOUR:
                    _run_basketball("--grade")
                    done.add(nba_key)
                elif now.hour in NBA_COLLECT_HOURS:
                    _run_basketball("--collect")
                    done.add(nba_key)
            if len(done) > 60:
                done.clear()
        except Exception as e:
            # Never let a bad run kill the thread.
            print(f"[scheduler] run failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(600)
