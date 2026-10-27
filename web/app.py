"""Always-on dashboard + collection scheduler.

Replaces the Railway cron service. A Railway volume attaches to exactly ONE service,
so the dashboard cannot be a separate service reading the collection volume — the two
have to be the same process. That is also what makes the volume reachable at all:
`railway volume files` tunnels over SFTP and needs a live container, which a cron
service only is for ~2 minutes per run.

Shows research/calibration data ONLY. bets.db (real money) never leaves the local
machine and is not deployed, so there is nothing here that can mix real and simulated.
"""
import os
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

from pydantic import BaseModel
from datetime import datetime, timedelta, timezone

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import subprocess
import sys

# The pipeline is written to run from inside baseball/ — its default DB and cache paths
# are relative. Put it on sys.path AND keep the directory, so relative paths can be
# resolved against it rather than against whatever CWD uvicorn happened to start in.
BASEBALL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "baseball"))
sys.path.insert(0, BASEBALL_DIR)
# basketball/ is deliberately NOT on sys.path: it is only ever run as a subprocess (see
# _run_basketball), and its data is read with plain sqlite3 (see _nba_db).
BASKETBALL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "basketball"))

# uvicorn imports this as `web.app` with the REPO ROOT on sys.path, not web/ — so a bare
# `from users import ...` below would not resolve. Add our own directory explicitly so
# the import works however the app is launched (uvicorn, pytest, python -m).
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
    # research.py owns the predictions schema, and its migrations run inside _connect().
    # This process READS that table through _db() (a bare sqlite3.connect), so without
    # this it would serve a pre-migration schema until the first collection run of the
    # day wrote a row — a window in which any endpoint selecting a newly added column
    # fails with "no such column". Opening once through the owning module closes it.
    try:
        from output.research import _connect as _research_connect
        # Must pass the RESOLVED path: research.py's default is relative and would
        # resolve against this process's CWD, migrating a different file than _db()
        # reads — see the note in _research_db_path().
        _research_connect(_research_db_path()).close()
        print("[research] schema ensured", flush=True)
    except Exception as e:
        print(f"[research] schema ensure failed: {type(e).__name__}: {e}", flush=True)
    # The same window exists for the NBA file, and this process can close it even less:
    # it opens that file read-only, and basketball/ cannot be imported here. So the
    # collector's own code applies its migrations, in a subprocess.
    try:
        if _run_basketball("--migrate") == 0:
            print("[nba] schema ensured", flush=True)
    except Exception as e:
        print(f"[nba] schema ensure failed: {type(e).__name__}: {e}", flush=True)
    threading.Thread(target=_scheduler, daemon=True).start()
    print("[scheduler] started", flush=True)
    yield


app = FastAPI(title="MLB Model Research", lifespan=lifespan)

# Users live in a SQLite store on the volume (web/users.py), NOT in env vars.
# DASH_USERS / DASH_PASS remain a first-run SEED only — see users.bootstrap_from_env.
from users import authenticate, bootstrap_from_env   # noqa: E402  (needs sys.path above)

# Auth is form + signed cookie, not HTTP Basic. Basic forces the browser's own popup
# (no custom login screen) and has no real sign-out — the browser caches credentials for
# the window. A signed session cookie fixes both: it can be cleared on demand, and the
# login screen is ours to design.
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
    """Stateless signed token: <username>|<expiry>|<signature>. No server-side store to
    keep in sync, and tampering with either field invalidates the signature."""
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
    """(username, role) for a valid session, else None. Never raises — callers decide
    whether anonymous is acceptable, so the same helper serves public and private routes."""
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
    """Admin-only gate. Applied to the DATA endpoint, not just the page — hiding a route
    in the UI while the API still answers is not access control.
    """
    if who[1] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return who


def _research_db_path() -> str:
    """Absolute path to research.db.

    RESEARCH_DB is absolute on Railway (/data/research.db) but relative by default
    (data/history/research.db). A relative path resolves against the process CWD, which
    for uvicorn is the repo root rather than baseball/ — so anchor relative paths to
    BASEBALL_DIR and leave absolute ones alone.
    """
    from output.research import RESEARCH_DB
    return RESEARCH_DB if os.path.isabs(RESEARCH_DB) else os.path.join(BASEBALL_DIR, RESEARCH_DB)


def _db():
    conn = sqlite3.connect(_research_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def stats(db=_db) -> dict:
    """Calibration + ranking signal. Mirrors output/research.py report() — and its NBA
    twin, which computes the same figures from the same model_prob/outcome columns, so
    one implementation serves both sports. `db` is the connection factory."""
    try:
        c = db()
        rows = [dict(r) for r in c.execute(
            "SELECT model_prob p, outcome, date FROM predictions "
            # NBA logs rows with no probability (no usable form), and a NULL would crash
            # the sums below. MLB has no such graded rows, so this changes nothing there.
            "WHERE outcome IN ('win','loss') AND model_prob IS NOT NULL")]
        total = c.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        by_day = [dict(r) for r in c.execute(
            "SELECT date, COUNT(*) n, SUM(outcome IN ('win','loss')) graded "
            "FROM predictions GROUP BY date ORDER BY date DESC LIMIT 10")]
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

    rows.sort(key=lambda r: r["p"])
    half = n // 2
    if half:
        lo, hi = hit(rows[:half]), hit(rows[half:])
        out["lower"], out["upper"], out["gap"] = lo, hi, hi - lo
        # Ship the confidence interval, not just the point estimate. A +6.9pt gap on 696
        # rows has a CI of [-0.2, +14.0] — indistinguishable from zero — and a label that
        # reads only the point estimate calls that "weak signal", asserting a finding the
        # data cannot support.
        import math
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
    """A real sign-out — the cookie is the entire session, so clearing it ends it."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    """Who am I? Returns null when anonymous rather than 401 — the front-end uses this to
    decide between the login screen and the dashboard, and a 401 here would be noise."""
    who = current_user(request)
    return {"user": who[0], "role": who[1]} if who else {"user": None, "role": None}


@app.get("/api/summary")
def api_summary(who: tuple = Depends(auth)):
    """Headline numbers any signed-in user may see. Deliberately excludes buckets,
    ranking gap and slope — those are admin-only via /api/report."""
    s = stats()
    return {"logged": s.get("logged", 0), "graded": s.get("graded", 0),
            "actual": s.get("actual"), "predicted": s.get("predicted"),
            "role": who[1], "user": who[0], "error": s.get("error")}


@app.get("/api/slate")
def api_slate(_: tuple = Depends(auth), limit: int = 200):
    """Today's ungraded predictions, ranked — the pool a parlay gets built from.

    Returns the model's RANKING, not a recommendation. Two numbers per leg:
      model_prob      raw output, what research.db logs
      calibrated      what the betting gate actually uses

    They differ a lot on purpose: the measured calibration slope is well under 1, so the
    raw number overstates badly at the top end.

    `calibrated` is the STORED calibrated_prob and nothing else. Rows predating the
    column (before 2026-09-30) report null rather than a recomputation: recomputing
    applies today's CALIBRATION_SLOPE to a prediction that was gated under the old one,
    which silently rewrites what the model is claimed to have said. This endpoint serves
    the pending slate, so in practice every row has it.

    `clears_gate` is the PER-LEG EV gate (2026-10-12): edge against that leg's own price,
    not a probability threshold. When a leg is blocked, `blocked_by` gives a stable code
    and `blocked_detail` the numbers. Note `gate.live` — the gate is deliberately not
    validated yet, so every leg reports blocked until an out-of-sample test passes.
    """
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
        # Same gate the pipeline uses, so the dashboard can never disagree with what
        # actually surfaces. `blocked_by` carries the reason — an unexplained empty slate
        # is indistinguishable from a broken gate, which is how the old one stayed dead.
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
    """Recent winning legs for the ambient feed.

    research.db only — these are model predictions that hit, NOT real parlays. Winning
    parlays live in bets.db, which is deliberately never deployed, so the feed cannot
    accidentally present simulated results as real money.
    """
    try:
        c = _db()
        rows = [dict(r) for r in c.execute(
            "SELECT batter_name, team, actual_hits, model_prob, date "
            "FROM predictions WHERE outcome='win' AND batter_name IS NOT NULL "
            "ORDER BY date DESC, model_prob DESC LIMIT ?", (min(limit, 200),))]
        c.close()
    except Exception as e:
        return {"error": str(e), "hits": []}
    # PUBLIC — this feeds the login screen background, so it must work signed-out.
    # Player names and hit counts are public MLB box-score facts. The model's predicted
    # probability is NOT, so it is only included for signed-in users.
    signed_in = current_user(request) is not None
    return {"error": None, "hits": [
        {"name": r["batter_name"], "team": r["team"], "hits": r["actual_hits"],
         "date": r["date"], **({"prob": r["model_prob"]} if signed_in else {})}
        for r in rows]}


@app.get("/admin", response_class=HTMLResponse)
def admin():
    """Same SPA — the front-end branches on pathname. Kept as its own route so the
    server serves index.html here instead of 404ing on a client-side route."""
    return index()


@app.get("/nba", response_class=HTMLResponse)
@app.get("/nba/admin", response_class=HTMLResponse)
def nba_pages():
    """Same SPA, for the same reason as /admin."""
    return index()


@app.get("/api/report")
def api_report(_: tuple = Depends(require_admin)):
    return stats()


# ── NBA research views ───────────────────────────────────────────────────────
# Read with plain sqlite3, never by importing basketball/. This process has BASEBALL_DIR
# on sys.path, so `output.research` would resolve to BASEBALL's module and `parlay.method`
# would not exist — the name collision _run_basketball documents. The cost is restating
# the two small things needed from basketball/; each names its twin.

def _nba_db_path() -> str:
    """Absolute path to the NBA research DB. Twin of RESEARCH_DB in
    basketball/output/research.py, anchored to BASKETBALL_DIR the way _research_db_path()
    anchors baseball's: the collector subprocess runs with that as its cwd."""
    p = os.getenv("NBA_RESEARCH_DB_PATH") or "data/history/research.db"
    return p if os.path.isabs(p) else os.path.join(BASKETBALL_DIR, p)


def _nba_db():
    """READ-ONLY. The collector owns this file and its schema (migrated at startup via
    `main.py --migrate`), and nothing on the web side should write to it. Read-only also
    makes a missing file an error, rather than a new empty database at a misconfigured
    path. as_uri() because the local path contains a space."""
    conn = sqlite3.connect(f"{Path(_nba_db_path()).as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _nba_stale_cutoff() -> str:
    """Slate date before which every prediction should already be graded.

    Today's slate is Eastern, a fixed UTC-5 — twin of slate_date() in
    basketball/pipeline/calendar.py. The 11:00 UTC run grades the previous slate, so
    yesterday's rows can legitimately still be pending; anything OLDER means grading is
    stuck. It was, silently, for six days (2026-10-20..26) while collection looked healthy.
    """
    today = datetime.now(timezone.utc) - timedelta(hours=5)
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")


@app.get("/api/nba/summary")
def api_nba_summary(who: tuple = Depends(auth)):
    """Headline NBA numbers for any signed-in user, plus what the page needs in order to
    say when grading is behind. Buckets, ranking gap and collection health are admin-only
    via /api/nba/report."""
    s = stats(_nba_db)
    out = {"logged": s.get("logged", 0), "graded": s.get("graded", 0),
           "actual": s.get("actual"), "predicted": s.get("predicted"),
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
    """Every prop logged for one slate (default: the latest), ranked by model probability.

    No edge column, unlike the MLB slate. NBA has no fitted calibration (calibrated_prob
    is NULL by design), and model_prob minus the book's implied probability would present
    an edge the model has not demonstrated. Both numbers are shown and the reader compares.
    Graded rows carry their outcome, so a past slate doubles as its own results page.

    Players listed OUT are ranked last. The model logs injury status but does not use it,
    so an out player's stale low line scores near-certain — on 10-26 the two top rows were
    out players at 98% — and a DNP voids anyway. Display order only; every row is returned.
    """
    try:
        c = _nba_db()
        day = day or c.execute("SELECT MAX(date) FROM predictions").fetchone()[0]
        rows = [dict(r) for r in c.execute(
            "SELECT player_name, team, opponent, market, line, book_odds, book_implied, "
            "       base_prob, owner_adj, model_prob, hit_l10, injury_status, outcome, "
            "       actual_value "
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
         "outcome": r["outcome"], "actual": r["actual_value"]}
        for r in rows]}


@app.get("/api/nba/parlays")
def api_nba_parlays(_: tuple = Depends(auth), limit: int = 40):
    """The method's paper parlays, newest first, with their legs and a per-tier record.

    Paper only: these live in the research DB and nothing was staked, so they sit beside
    the simulated predictions and never beside bets.db (§14). `staked` counts settled
    win/loss parlays only — a void returns its stake and is neither.
    """
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
                "       undercut, odds, hit_l10, hit_l20, outcome, actual_value "
                f"FROM method_legs WHERE parlay_id IN ({','.join('?' * len(ids))}) "
                "ORDER BY id", ids) if ids else []):
            legs.setdefault(r["parlay_id"], []).append({
                "name": r["player_name"], "team": r["team"], "opp": r["opponent"],
                "market": r["market"], "line": r["line"], "main_line": r["main_line"],
                "undercut": r["undercut"], "odds": r["odds"], "l10": r["hit_l10"],
                "l20": r["hit_l20"], "outcome": r["outcome"], "actual": r["actual_value"]})
        record = [dict(r) for r in c.execute(
            "SELECT tier, SUM(outcome='win') won, SUM(outcome='loss') lost, "
            "       SUM(outcome='void') void, "
            "       SUM(CASE WHEN outcome IN ('win','loss') THEN stake ELSE 0 END) staked, "
            "       SUM(COALESCE(profit, 0)) profit "
            "FROM method_parlays WHERE outcome IS NOT NULL "
            "GROUP BY tier ORDER BY MIN(stake)")]
        c.close()
    except Exception as e:
        return {"error": str(e), "record": [], "parlays": []}
    return {"error": None, "record": record, "parlays": [
        {**{k: p[k] for k in ("date", "tier", "stake", "n_legs", "american_odds",
                               "profit_if_win", "first_tip", "outcome", "profit")},
         "legs": legs.get(p["id"], [])}
        for p in parlays]}


@app.get("/api/nba/report")
def api_nba_report(_: tuple = Depends(require_admin)):
    """NBA calibration (the shared stats()) plus two NBA-only tables.

    `health` is collection AND grading per slate, and the page renders it even when
    nothing is graded. MLB's admin page hides its by-day table in exactly that case, which
    is the one case where it matters: NBA collected for six days while every grading run
    settled zero rows, and nothing on any screen said so.
    """
    out = stats(_nba_db)
    if out.get("error"):
        return out
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
            # outcome IN (...) is NULL, not 0, on an ungraded row, so a slate with nothing
            # graded would SUM to NULL — exactly the slate this table exists to show as 0.
            "       COALESCE(SUM(outcome IN ('win','loss')), 0) graded, "
            "       COALESCE(SUM(outcome = 'void'), 0) void, "
            "       SUM(outcome IS NULL) pending "
            "FROM predictions GROUP BY date ORDER BY date DESC LIMIT 30")]
        c.close()
    except Exception as e:
        out["error"] = str(e)
    return out


FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")

# Assets are unauthenticated on purpose: they are compiled JS/CSS with no data in them,
# and browsers do not reliably attach Basic credentials to subresource requests. Every
# piece of actual data goes through /api/report, which does require auth.
if os.path.isdir(os.path.join(FRONTEND_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")),
              name="assets")


@app.get("/", response_class=HTMLResponse)
def index():
    """Serves the built React app. Auth here is what makes the browser prompt once,
    after which it reuses the credentials for the /api/report fetch."""
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

# NBA runs later than MLB: tips are 23:00-03:00 UTC (7pm-10pm ET), so collection sits a
# couple of hours earlier and grading waits until every West Coast game has finished.
NBA_COLLECT_HOURS = {int(h) for h in
                     (os.getenv("NBA_COLLECT_HOURS") or "21,22,23,0,1").split(",")}
NBA_GRADE_HOUR = int(os.getenv("NBA_GRADE_HOUR") or 11)
NBA_ENABLED = (os.getenv("NBA_ENABLED") or "1") not in ("0", "false", "False")
# Pause switch for MLB in the offseason. An empty COLLECT_HOURS can't do it — `or` treats
# "" as unset and falls back to the default hours — so it needs its own flag.
MLB_ENABLED = (os.getenv("MLB_ENABLED") or "1") not in ("0", "false", "False")


def _run_basketball(arg: str) -> int:
    """Run the NBA collector as a SUBPROCESS, not an import.

    basketball/ deliberately duplicates baseball/ rather than sharing a package, so both
    define `main`, `output.research` and `pipeline.*`. This process already has
    BASEBALL_DIR on sys.path, and Python caches modules by name — so an in-process
    `import main` here would silently return BASEBALL's module and collect the wrong
    sport into the wrong database. A subprocess with its own cwd gets a clean namespace
    and makes the relative data/ paths resolve correctly, and an NBA crash cannot take
    the web process down with it.
    """
    proc = subprocess.run([sys.executable, "main.py", arg], cwd=BASKETBALL_DIR,
                          capture_output=True, text=True, timeout=1800)
    for line in (proc.stdout or "").splitlines()[-25:]:
        print(f"[nba] {line}", flush=True)
    if proc.returncode != 0:
        print(f"[nba] {arg} exited {proc.returncode}: "
              f"{(proc.stderr or '').strip()[-400:]}", flush=True)
    return proc.returncode


def _scheduler():
    """Replaces Railway cron. Collection is idempotent (_already_collected skips games
    already captured, and log_predictions upserts), so a duplicate pass is cheap rather
    than harmful — which is what makes a simple hourly check safe."""
    done: set[str] = set()
    while True:
        try:
            now = datetime.now(timezone.utc)
            key = f"{now:%Y-%m-%d}-{now.hour}"
            if MLB_ENABLED and key not in done:
                if now.hour == GRADE_HOUR:
                    from output.research import grade_predictions
                    grade_predictions()
                    done.add(key)
                elif now.hour in COLLECT_HOURS:
                    from main import _collect_predictions
                    _collect_predictions()
                    done.add(key)

            # NBA keyed separately: its hours overlap MLB's, and sharing `done` would let
            # whichever sport ran first mark the hour complete for both.
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
            # Never let a bad run kill the thread — that would silently stop collection
            # for the rest of the season with the web server still looking healthy.
            # This now covers two sports, so one sport failing must not stop the other.
            print(f"[scheduler] run failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(600)
