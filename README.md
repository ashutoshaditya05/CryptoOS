# TradeOS

TradeOS stores historical Bitcoin spot candles from Delta Exchange India (`BTC_INR`) and replays a versioned EMA pullback strategy. It uses Python 3.12+, SQLite, Parquet, and FastAPI. Data ingestion uses the exchange’s public API without credentials. [Delta API documentation](https://docs.delta.exchange/#spot-trading)

## Run locally

Install Python 3.12 and `uv`, then run the following commands from the project directory.

```powershell
uv sync --locked
uv run alembic upgrade head
uv run python -m app.backfill --timeframe 5m --start 2026-11-22T12:00:00Z --end 2026-11-22T13:00:00Z
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use a second PowerShell window to query the local API.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health'
Invoke-RestMethod 'http://127.0.0.1:8000/markets'
Invoke-RestMethod 'http://127.0.0.1:8000/candles?timeframe=5m&start=2026-11-22T12:00:00Z&end=2026-11-22T13:00:00Z'
```

Open [the local API docs](http://127.0.0.1:8000/docs) to run those requests in a browser. Candle queries accept up to 2,000 time slots and return stored candles sorted by opening time. Prices and volumes serialize as decimal strings. Unknown volume serializes as `null`.

## Backfill and resample

The CLI accepts `3m`, `5m`, `15m`, `1h`, and `4h`. Every range uses an inclusive start and exclusive end and must align to the selected UTC timeframe. Supply an explicit timezone offset or `Z`. Backfills stop at the latest closed boundary, split requests into pages, and report how many candles were inserted or already present and which intervals are missing. Identical rows can be replayed safely. A conflicting row fails its page and leaves prior committed pages available for retry. Missing candles stay missing.

This command derives `15m` candles from the locally stored `5m` backfill above.

```powershell
uv run python -m app.backfill --timeframe 15m --start 2026-11-22T12:00:00Z --end 2026-11-22T13:00:00Z --resample-from 5m
```

Resampling uses complete sets of closed source candles. If any source volume is unknown, the derived volume is unknown. Use one method per target series and date range in a dataset. Switching between native and derived candles can surface conflicts when their values differ.

The default data directory is `data/` relative to where you run the commands. Set `TRADEOS_DATA_DIR` for a separate dataset and run the migration there before using it. The SQLite manifest is `tradeos.sqlite3`. Candle files are in `candles/<timeframe>/`. See the [market data decision record](docs/adr/0001-delta-india-market-data.md) for storage details.

## Replay Bitcoin signals

`ema_pullback@1.0.0` evaluates closed `15m` candles using context from closed `1h` candles. It needs at least 50 consecutive `15m` candles and 200 consecutive `1h` candles. It uses up to 150 and 600 respectively to warm the indicators before each decision. This example loads both timeframes and evaluates the final hour.

```powershell
uv run python -m app.backfill --timeframe 1h --start 2026-10-28T00:00:00Z --end 2026-11-22T12:00:00Z
uv run python -m app.backfill --timeframe 15m --start 2026-11-20T12:00:00Z --end 2026-11-22T12:00:00Z
uv run python -m app.signals --start 2026-11-22T11:00:00Z --end 2026-11-22T12:00:00Z
```

With the API running, use the same window at `/signals`.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/signals?start=2026-11-22T11:00:00Z&end=2026-11-22T12:00:00Z'
```

Ranges use the same candle boundaries as `/candles`. Signal timestamps are the selected candles’ close times, so a `start` of `11:00Z` and `end` of `12:00Z` returns closes from `11:15Z` through `12:00Z`. Each signal expires at the next `15m` close and includes its strategy version, settings fingerprint, market regime, and reason codes. A missing `15m` candle produces no signal. If the warm-up is incomplete, available candles produce HOLD until there is enough contiguous history.

When flat, BUY requires `1h` EMA50 above EMA200, `15m` EMA20 above EMA50, and a `15m` candle with a low at or below its EMA20 and a close above it. ADX and ATR fraction filter entries, and ATR sets the suggested stop distance. When evaluating an existing long, SELL requires either EMA trend to lose its uptrend. `--position long` or `position=long` applies that position assumption to every candle in the query. Signals are read-only and contain reference levels for later backtesting.

The defaults are `min_adx=20`, `max_atr_fraction=0.03`, `atr_stop_multiple=1.5`, and `reward_risk_ratio=2`. These settings are research defaults. Override them with CLI flags such as `--min-adx 25` or API parameters such as `min_adx=25`.

### Three-minute VWAP and EMA9

`vwap_ema_cross@1.0.0` implements the rules narrated in the Reel for long-only Bitcoin spot. Daily VWAP uses `(high + low + close) / 3` weighted by candle volume and starts at midnight UTC for the 24-hour market. EMA9 uses consecutive three-minute closes.

- **BUY** when flat and a closed candle’s lower wick touches or passes through daily VWAP, its open is at or above VWAP, and its body closes above EMA9 after opening at or below EMA9.
- **SELL** when evaluating an existing long and a three-minute candle closes below EMA9. Use `--position long` or `position=long` to evaluate exits.
- **HOLD** in other cases. A bar needs at least nine consecutive closes to evaluate either side. Entries also need complete candles from midnight UTC and known session volume with a positive total.

Fetch the session and EMA warm-up data, then replay the requested Bitcoin hour.

```powershell
uv run python -m app.backfill --timeframe 3m --start 2026-11-21T11:00:00Z --end 2026-11-22T12:00:00Z
uv run python -m app.signals --strategy-version vwap_ema_cross@1.0.0 --start 2026-11-22T11:00:00Z --end 2026-11-22T12:00:00Z
```

The API accepts `strategy_version=vwap_ema_cross%401.0.0` on `/signals`. The sample command evaluates each candle from a flat position.

## Backtest Bitcoin spot

The backtest starts flat with virtual INR cash, replays the selected strategy using the position after simulated fills, and saves the order ledger, closed trades, candle-close equity, and a buy-and-hold benchmark. The default strategy is `vwap_ema_cross@1.0.0`. A single replay supports up to 200,000 candle slots, including a full year of three-minute candles. Run the migration before using it on an existing dataset.

```powershell
uv run alembic upgrade head
uv run python -m app.backtest --start 2026-11-22T11:00:00Z --end 2026-11-22T12:00:00Z
```

Use `--get <run-id> --full` to read all saved details locally. The API accepts the same request at `POST /backtests` and returns saved reports at `GET /backtests/{run_id}`. Set `--strategy-version ema_pullback@1.0.0` to run the 15-minute pullback. To customize execution, pass `--config` with a JSON request containing `start`, `end`, `strategy_version`, and `simulation` fields. All funds, fees, and prices in the report are denominated in INR unless the field specifies BTC.

### Fill assumptions

- A strategy signal can fill at the next scheduled candle open. A gap or the end of the window cancels that order. Available size is capped by a fraction of the preceding closed candle’s BTC volume, and a partial market fill cancels its remainder.
- The default model starts with ₹100,000, allocates up to 25% of cash per entry, and uses a 10 bps full spread, 5 bps slippage per side, a ₹0.01 price tick, and a 0.000001 BTC quantity step. These are simulation assumptions. Minimum INR notional and quantity controls can be configured in `simulation`.
- The default buy fee is 0 bps and sell fee is 10 bps based on Delta’s published spot schedule. The model omits taxes and withholding. [Delta spot fees](https://www.delta.exchange/support/solutions?articleId=80001194210&categoryId=80000464973&folderId=80000726064)
- For a strategy with protective levels, a stop fills against a gap down at the open. If both levels are touched intrabar and the opening event cannot resolve their order, the stop takes priority. Intrabar fills carry the candle close as a reporting timestamp. Open inventory is marked at the last observed close.
- Buy-and-hold uses the same starting cash, allocation, liquidity cap, and entry costs. It enters at the first open that supports a fill and marks that position at the end. The report records its first fill time.

### Research mode

This longer example uses independent chronological windows, walk-forward evaluation, cost sensitivity, and a seeded trade-block bootstrap when there are enough completed trades. It needs the complete day of warm-up before November 8 for the first UTC VWAP session.

```powershell
uv run python -m app.backfill --timeframe 3m --start 2026-11-07T00:00:00Z --end 2026-11-22T12:00:00Z
uv run python -m app.backtest --start 2026-11-08T00:00:00Z --end 2026-11-22T12:00:00Z --research
```

The 60/20/20 train, validation, and holdout windows start independently with cash and can warm indicators from earlier closed candles. Walk-forward windows select eligible pullback parameter candidates on earlier data only, and end before the final holdout. Configure candidates through `research.pullback_candidates` in a JSON request. The Reel strategy has fixed signal rules. Cost cases compare zero bps, the base assumptions, and higher costs. The bootstrap resamples consecutive completed-trade return blocks and estimates return and drawdown quantiles.

The report includes the input candle and code hashes. Returns and drawdown use candle-close marks. Sharpe and Sortino use consecutive UTC daily endpoints with a zero risk-free rate and show `null` when the denominator is unavailable. Daily historical VaR is shown only after 20 daily observations, and the bootstrap requires at least five completed trades. These estimates describe the tested sample.

## Verify offline

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use synthetic candles, a mocked Delta API, and a frozen signal fixture to verify ingestion, storage, resampling, signal reproducibility, and HTTP retrieval with temporary databases.
