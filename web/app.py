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
from datetime import datetime, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import sys

# The pipeline is written to run from inside baseball/ — its default DB and cache paths
# are relative. Put it on sys.path AND keep the directory, so relative paths can be
# resolved against it rather than against whatever CWD uvicorn happened to start in.
BASEBALL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "baseball"))
sys.path.insert(0, BASEBALL_DIR)

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


def stats() -> dict:
    """Calibration + ranking signal. Mirrors output/research.py report()."""
    try:
        c = _db()
        rows = [dict(r) for r in c.execute(
            "SELECT model_prob p, outcome, date FROM predictions "
            "WHERE outcome IN ('win','loss')")]
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


@app.get("/api/report")
def api_report(_: tuple = Depends(require_admin)):
    return stats()


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


def _scheduler():
    """Replaces Railway cron. Collection is idempotent (_already_collected skips games
    already captured, and log_predictions upserts), so a duplicate pass is cheap rather
    than harmful — which is what makes a simple hourly check safe."""
    done: set[str] = set()
    while True:
        try:
            now = datetime.now(timezone.utc)
            key = f"{now:%Y-%m-%d}-{now.hour}"
            if key not in done:
                if now.hour == GRADE_HOUR:
                    from output.research import grade_predictions
                    grade_predictions()
                    done.add(key)
                elif now.hour in COLLECT_HOURS:
                    from main import _collect_predictions
                    _collect_predictions()
                    done.add(key)
            if len(done) > 60:
                done.clear()
        except Exception as e:
            # Never let a bad run kill the thread — that would silently stop collection
            # for the rest of the season with the web server still looking healthy.
            print(f"[scheduler] run failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(600)
