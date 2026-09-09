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
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

import sys

# The pipeline is written to run from inside baseball/ — its default DB and cache paths
# are relative. Put it on sys.path AND keep the directory, so relative paths can be
# resolved against it rather than against whatever CWD uvicorn happened to start in.
BASEBALL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "baseball"))
sys.path.insert(0, BASEBALL_DIR)

@asynccontextmanager
async def lifespan(_: "FastAPI"):
    threading.Thread(target=_scheduler, daemon=True).start()
    print("[scheduler] started", flush=True)
    yield


app = FastAPI(title="MLB Model Research", lifespan=lifespan)
security = HTTPBasic()

DASH_USER = os.getenv("DASH_USER") or "kai"
DASH_PASS = os.getenv("DASH_PASS")          # unset => dashboard refuses all requests


def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    """Constant-time comparison. A plain == leaks the password through timing."""
    if not DASH_PASS:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "DASH_PASS not configured — dashboard disabled")
    ok_u = secrets.compare_digest(creds.username, DASH_USER)
    ok_p = secrets.compare_digest(creds.password, DASH_PASS)
    if not (ok_u and ok_p):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials",
                            {"WWW-Authenticate": "Basic"})
    return creds.username


def _research_db_path() -> str:
    """Absolute path to research.db.

    RESEARCH_DB is absolute on Railway (/data/research.db) but relative by default
    (data/history/research.db). A relative path resolves against the process CWD, which
    for uvicorn is the repo root, not baseball/ — so it silently pointed at a file that
    does not exist. Anchor relative paths to BASEBALL_DIR instead.
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


@app.get("/api/report")
def api_report(_: str = Depends(auth)):
    return stats()


FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")

# Assets are unauthenticated on purpose: they are compiled JS/CSS with no data in them,
# and browsers do not reliably attach Basic credentials to subresource requests. Every
# piece of actual data goes through /api/report, which does require auth.
if os.path.isdir(os.path.join(FRONTEND_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")),
              name="assets")


@app.get("/", response_class=HTMLResponse)
def index(_: str = Depends(auth)):
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
