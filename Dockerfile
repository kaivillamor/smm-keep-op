# Multi-stage: node builds the React front-end, python serves it alongside the API.
# Not nixpacks: it bakes every service variable, API keys included, into image layers.

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

# Dependencies first, so this layer caches across code pushes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /fe/dist ./web/frontend/dist

# Always-on: serves the dashboard and runs the collection scheduler.
CMD ["sh", "-c", "uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
