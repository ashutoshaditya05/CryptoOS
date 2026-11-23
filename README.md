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

The CLI accepts `5m`, `15m`, `1h`, and `4h`. Every range uses an inclusive start and exclusive end and must align to the selected UTC timeframe. Supply an explicit timezone offset or `Z`. Backfills stop at the latest closed boundary, split requests into pages, and report how many candles were inserted or already present and which intervals are missing. Identical rows can be replayed safely. A conflicting row fails its page and leaves prior committed pages available for retry. Missing candles stay missing.

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

## Verify offline

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use synthetic candles, a mocked Delta API, and a frozen signal fixture to verify ingestion, storage, resampling, signal reproducibility, and HTTP retrieval with temporary databases.
