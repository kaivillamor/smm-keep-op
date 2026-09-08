# Explicit Dockerfile instead of nixpacks. Three reasons:
#
#  1. Secrets. Nixpacks injects every service variable as Docker ARG/ENV at build time,
#     baking PROP_ODDS_API_KEY into the image layers (the SecretsUsedInArgOrEnv warning).
#     Nothing here declares them, so they exist only in the running container, where
#     Railway injects them at start. Encryption is NOT an alternative fix: anything the
#     app can decrypt unaided needs its key in the image too.
#  2. PATH. Nixpacks' generated Dockerfile references $NIXPACKS_PATH before defining it,
#     which is why `python` did not resolve in a non-login shell. A normal python base
#     image has it on PATH already.
#  3. Reproducibility. The base image is pinned; builds skip the nix layer entirely.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilds when requirements change,
# so an ordinary code push doesn't reinstall pandas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cron service: runs once and exits. Railway's restart policy must stay NEVER.
CMD ["bash", "scripts/collect.sh"]
