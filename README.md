# TradeOS

TradeOS Phase 1 stores historical Bitcoin spot candles from Delta Exchange India (`BTC_INR`). It uses Python 3.12+, SQLite, Parquet, and FastAPI. Data ingestion uses the exchange’s public API without credentials. [Delta API documentation](https://docs.delta.exchange/#spot-trading)

## Run locally

From the project directory, with Python 3.12 and `uv` installed:

```powershell
uv sync --locked
uv run alembic upgrade head
uv run python -m app.backfill --timeframe 5m --start 2026-11-22T12:00:00Z --end 2026-11-22T13:00:00Z
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second PowerShell window:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health'
Invoke-RestMethod 'http://127.0.0.1:8000/markets'
Invoke-RestMethod 'http://127.0.0.1:8000/candles?timeframe=5m&start=2026-11-22T12:00:00Z&end=2026-11-22T13:00:00Z'
```

Open [the local API docs](http://127.0.0.1:8000/docs) to run those requests in a browser. Candle queries accept up to 2,000 time slots and return stored candles sorted by opening time. Prices and volumes serialize as decimal strings. Unknown volume serializes as `null`.

## Backfill and resample

The CLI accepts `5m`, `15m`, `1h`, and `4h`. Every range uses an inclusive start and exclusive end and must align to the selected UTC timeframe. Supply an explicit timezone offset or `Z`. Backfills stop at the latest closed boundary, split requests into pages, and report how many candles were inserted or already present and which intervals are missing. Identical rows can be replayed safely. A conflicting row fails its page and leaves prior committed pages available for retry. Missing candles stay missing.

After the `5m` backfill above, you can build `15m` candles from the local store:

```powershell
uv run python -m app.backfill --timeframe 15m --start 2026-11-22T12:00:00Z --end 2026-11-22T13:00:00Z --resample-from 5m
```

Resampling uses complete sets of closed source candles. If any source volume is unknown, the derived volume is unknown. Use one method per target series and date range in a dataset. Switching between native and derived candles can surface conflicts when their values differ.

The default data directory is `data/` relative to where you run the commands. Set `TRADEOS_DATA_DIR` for a separate dataset and run the migration there before using it. The SQLite manifest is `tradeos.sqlite3`. Candle files are in `candles/<timeframe>/`. See the [market data decision record](docs/adr/0001-delta-india-market-data.md) for storage details.

## Verify offline

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use synthetic candles and a mocked Delta API to verify ingestion, storage, resampling, and HTTP retrieval with temporary databases.
