# smm-keep-op

A sports analytics and betting model for identifying value opportunities in the MLB and
NBA markets.

It builds its own probabilities from stats and matchups, compares them against
sportsbook lines, and surfaces the disagreements. It then measures whether those
probabilities actually mean anything before any money is risked.

## What it does

### MLB

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

### NBA — collecting

- **Player props.** Points, rebounds, assists and made threes. Every player with a posted
  line is scored against it from their own game log (season and recent form, minutes, a
  usage proxy, and the spread of their own results), then graded against the box score.
- Collection started on opening night, 2026-10-20. There is deliberately no betting gate
  yet: the model gets measured first.

## Model validation

The part that decides whether any of the above is worth betting. A separate research
database scores every player on a slate, not just the ones the model likes, grades them
against box scores, and answers one question: does a higher model probability actually
produce a better result?

It reports:

- **Calibration.** Predicted vs actual, bucketed, so systematic over or under confidence
  is visible.
- **Ranking signal.** Hit rate of the model's top half against its bottom half, with a
  95% confidence interval. A verdict is only reported when that interval excludes zero,
  and it is judged on the interval's lower bound rather than the point estimate, so more
  data can't make the verdict worse on its own.
- **A/B against hand-written rules.** Personal adjustments are stored separately from the
  quant output, so both can be scored on identical outcomes as a paired test.

And it guards the data it measures:

- **Verified price feeds.** Every odds source is checked against the live sportsbook
  before any pricing conclusion is drawn from it, and every logged price records which
  book quoted it.
- **Every input is logged.** If a value feeds a prediction or a betting decision, it is
  stored as its own column, so a later refit can reproduce the model and nothing is
  measured against a number nobody can see.
- **Per-leg betting gate.** Bets are judged on a calibrated probability against that
  leg's own price, not a single threshold. The gate stays off until it passes an
  out-of-sample test.
- **Regime tagging.** Preseason and postseason rows are tagged by season phase, so they
  never pool silently into the corpus the model is judged on.

Simulated predictions live in their own databases and can never reach the real-money P&L.

## Dashboard

A small React front-end over a FastAPI service, deployed on Railway: calibration, ranking
signal, recent results, and the current slate with each leg's edge against its price,
laid out as dense reference tables. Session-cookie auth with scrypt-hashed credentials and
role-gated admin views.

Collection for both sports runs on a scheduler inside the same always-on service, so data
gathering doesn't depend on a local machine being awake. NBA runs as an isolated
subprocess, so one sport failing can't stop the other.

## Tech stack

- **Python 3.12**, SQLite, FastAPI
- **React 18 + Vite** for the dashboard front-end
- **Railway** for deployment, volume-backed storage, and scheduled collection
- **The Odds API** for game lines
- **Player-prop odds API** for MLB and NBA prop pricing
- **MLB Stats API** and **Baseball Savant** for baseball stats, lineups, and Statcast
- **ESPN** for NBA schedules, rosters, game logs, and box scores
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

### MLB

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

### NBA

Run from the `basketball/` directory. Research only, nothing here places a bet:

```bash
python main.py --collect           # score tonight's priced players and log them
python main.py --grade             # grade finished games from box scores
python main.py --collect-grade     # grade, then collect
python main.py --report            # calibration + ranking-signal report
```

## Results

**MLB hits.** Across roughly 2,600 graded regular-season predictions, the model's
higher-rated legs hit about 6 points more often than its lower-rated ones, with the 95%
confidence interval clear of zero. The ranking signal is real, but modest. The hand-tuned
model also beat a fitted logistic regression on the same inputs.

It still doesn't beat the book, and the validation layer found why: the model is
overconfident at the top. Its most confident picks were rated around 79% and hit about
64%, under the roughly 68% the sportsbook's price implied. Calibrated, its ceiling sits
below where books price their favourites, so no threshold makes the hit layer profitable
at current prices.

An earlier reading had it the other way round — that the market was simply priced past
the model. That one rested on a price feed that turned out to be mis-reporting one book,
which is why every feed is now verified against the live sportsbook before it's trusted.
Both conclusions came from the validation layer rather than the P&L, which is the reason
for building one.

**NBA.** Collection began 2026-10-20. No results yet.

## Disclaimer

Personal research project for modeling and analytics. Not financial advice, and not a
guarantee of results.
