# smm-keep-op

A sports analytics and betting model for identifying value opportunities in the MLB market.

It builds its own implied probabilities from stats, matchups, park, and weather, compares
them against sportsbook lines, and surfaces the disagreements — then tracks every pick in a
local database so performance can be measured over time.

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

## Tech stack

- **Python 3.11+**, SQLite
- **The Odds API** — game lines
- **Player-prop odds API** — hit/HR prop pricing
- **MLB Stats API** & **Baseball Savant** — stats, lineups, Statcast
- **OpenWeather** — ballpark conditions
- **OpenAI** — optional qualitative leg review

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
python output/backtest.py summary  # profit/loss summary
```

## Disclaimer

Personal research project for modeling and analytics. Not financial advice, and not a
guarantee of results.
