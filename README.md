# smm-keep-op

A sports analytics and betting model for identifying value opportunities in the MLB market.

It builds its own implied probabilities from stats, matchups, park, and weather, compares
them against sportsbook lines, and surfaces the disagreements. It then measures whether
those probabilities actually mean anything before any money is risked.

## What it does

- **Game-line value bets.** Models moneyline and total probabilities across a 3-layer
  pipeline (quant stats, then rules, then optional LLM review) and flags legs where the
  model disagrees with the book after removing vig, assembled into small parlays.
- **Hit parlays.** Estimates each batter's probability of getting 1+ hit, pairs the best
  into 2-leg parlays, and prices them against real sportsbook odds to rank by expected
  value.
- **HR props and parlays.** A factor gate (barrel rate, contact quality, zone fit,
  opposing pitcher HR tendency) surfaces home-run candidates and pairs them into 2-leg
  parlays.
- **Backtesting and P&L.** Every pick is logged to SQLite and graded against real game
  results, with a running profit/loss summary per bet type.

## Model validation

The part that decides whether any of the above is worth betting. A separate research
database scores every batter on a slate, not just the ones the model likes, grades them
against box scores, and answers one question: does a higher model probability actually
produce a higher hit rate?

It reports:

- **Calibration.** Predicted vs actual, bucketed, so systematic over or under confidence
  is visible.
- **Ranking signal.** Hit rate of the model's top half against its bottom half, with a
  95% confidence interval. A verdict is only reported when that interval excludes zero,
  so a thin sample can't be mistaken for an edge.
- **A/B against hand-written rules.** Personal adjustments are stored separately from the
  quant output, so both can be scored on identical outcomes as a paired test.

Simulated predictions live in their own database and can never reach the real-money P&L.

## Dashboard

A small React front-end over a FastAPI service, deployed on Railway, showing the model's
calibration, ranking signal, and recent results. Session-cookie auth with scrypt-hashed
credentials and role-gated admin views. The collection scheduler runs in the same
always-on service, so data gathering doesn't depend on a local machine being awake.

## Tech stack

- **Python 3.12**, SQLite, FastAPI
- **React 18 + Vite** for the dashboard front-end
- **Railway** for deployment, volume-backed storage, and scheduled collection
- **The Odds API** for game lines
- **Player-prop odds API** for hit and HR prop pricing
- **MLB Stats API** and **Baseball Savant** for stats, lineups, and Statcast
- **OpenWeather** for ballpark conditions (game-line model)
- **Anthropic** for optional qualitative leg review

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

## Results

Across roughly 1,300 graded predictions, the hit model's higher-rated legs hit 8.1 points
more often than its lower-rated ones, with the confidence interval clear of zero. The
hand-tuned model also beat a fitted logistic regression on the same inputs.

Being accurate turned out not to be enough to beat the book's pricing. That came from the
validation layer rather than the P&L, which is the reason for building one.

## Disclaimer

Personal research project for modeling and analytics. Not financial advice, and not a
guarantee of results.
