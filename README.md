# smm-keep-op

A sports analytics and betting model for identifying value opportunities in the MLB market.

It builds its own implied probabilities from stats, matchups, park, and weather, compares
them against sportsbook lines, and surfaces the disagreements — then **measures whether
those probabilities actually mean anything** before any money is risked.

## What it does

- **Game-line value bets** — models moneyline/total probabilities across a 3-layer pipeline
  (quant stats → rules → optional LLM review) and flags legs where the model disagrees with
  the book after removing vig, assembled into small parlays.
- **Hit parlays** — estimates each batter's probability of getting 1+ hit, pairs the best
  into 2-leg parlays, and prices them against real sportsbook odds to rank by expected value.
- **HR props & parlays** — a factor gate (barrel rate, contact quality, zone fit, opposing
  pitcher HR tendency) surfaces home-run candidates and pairs them into 2-leg parlays.
- **Backtesting & P&L** — every pick is logged to SQLite and graded against real game results,
  with a running profit/loss summary per bet type.

## Model validation

The part that decides whether any of the above is worth betting. A separate research
database scores **every** batter on a slate — not just the ones the model likes — grades
them against box scores, and answers one question: *does a higher model probability actually
produce a higher hit rate?*

It reports:

- **Calibration** — predicted vs actual, bucketed, so systematic over/under-confidence is visible
- **Ranking signal** — hit rate of the model's top half vs its bottom half, **with a 95%
  confidence interval**. A verdict is only reported when that interval excludes zero, so a
  thin sample can't be mistaken for an edge
- **A/B against hand-written rules** — personal adjustments are stored separately from the
  quant output, so both can be scored on identical outcomes as a paired test

Simulated predictions live in their own database and can never reach the real-money P&L.

## Dashboard

A small React front-end over a FastAPI service, deployed on Railway, showing the model's
calibration, ranking signal, and recent results. Session-cookie auth with scrypt-hashed
credentials and role-gated admin views. The collection scheduler runs in the same always-on
service, so data gathering doesn't depend on a local machine being awake.

## Tech stack

- **Python 3.12**, SQLite, FastAPI
- **React 18 + Vite** — dashboard front-end
- **Railway** — deployment, volume-backed storage, scheduled collection
- **The Odds API** — game lines
- **Player-prop odds API** — hit/HR prop pricing
- **MLB Stats API** & **Baseball Savant** — stats, lineups, Statcast
- **OpenWeather** — ballpark conditions (game-line model)
- **Anthropic** — optional qualitative leg review

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the API keys listed there
```

API keys live in `.env`, which is never committed.

## Usage

Run from the `baseball/` directory:

```bash
python main.py --no-llm            # game-line value parlays
python main.py --no-llm --hits-2   # hit parlays
python main.py --no-llm --props    # HR props & parlays
python main.py --results           # grade yesterday's picks (run next morning)
python main.py --usage             # odds API credit usage
python output/backtest.py summary  # profit/loss summary
```

Model validation (separate database, never touches the betting record):

```bash
python main.py --watch             # poll for lineups, collect each game as it confirms
python main.py --collect           # one-shot collection of the current slate
python main.py --collect-grade     # grade predictions + calibration report
python output/research.py report   # calibration + ranking-signal report
python output/research.py owner    # hand-written rules vs the quant model
```

## What the measurement found

Over ~1,300 graded predictions the hit model showed a **statistically real ranking signal**
(top half vs bottom half gap of +8.1 points, 95% CI [+2.4, +13.7]), and a hand-tuned
multiplicative model **outperformed a fitted logistic regression** on the same inputs under
leave-one-day-out cross-validation.

It also found that model accuracy alone doesn't make a market beatable — and that a price
feed should be verified against the live sportsbook before any pricing conclusion is drawn.
Both findings came from the validation layer rather than from the P&L, which is the point
of building one.

## Disclaimer

Personal research project for modeling and analytics. Not financial advice, and not a
guarantee of results.
