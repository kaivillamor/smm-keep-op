# Multi-stage: node builds the React front-end, python serves it alongside the API.
#
# Explicit Dockerfile rather than nixpacks because nixpacks injects every service
# variable as Docker ARG/ENV (baking PROP_ODDS_API_KEY into image layers) and its
# generated template references $NIXPACKS_PATH before defining it, which broke PATH.

# ---- stage 1: build the front-end -------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /fe
COPY web/frontend/package.json web/frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/frontend/ ./
RUN npm run build

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim
WORKDIR /app

# Dependencies first so this layer caches and an ordinary code push doesn't
# reinstall pandas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /fe/dist ./web/frontend/dist

# Always-on service: serves the dashboard AND runs collection on an internal schedule.
# A Railway volume attaches to only one service, so these cannot be split apart.
# Restart policy should be ON_FAILURE (not NEVER) — this is meant to stay up.
CMD ["sh", "-c", "uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
