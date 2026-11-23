import argparse
import sys
from datetime import datetime
from functools import partial

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.config import data_directory
from app.market.delta import DELTA_URL, MAX_CANDLES, DeltaCandleProvider, DeltaError
from app.market.ingestion import backfill
from app.market.models import BITCOIN, Timeframe, as_utc, candle_window
from app.market.resampling import fetch_resampled, resampling_ratio
from app.market.storage import CandleStore, StorageError


def parse_datetime(value: str) -> datetime:
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a timestamp such as 2026-11-22T12:00:00Z.") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill closed Delta India BTC_INR candles.")
    parser.add_argument("--timeframe", required=True, type=Timeframe, choices=list(Timeframe))
    parser.add_argument("--start", required=True, type=parse_datetime, help="Inclusive UTC start")
    parser.add_argument("--end", required=True, type=parse_datetime, help="Exclusive UTC end")
    parser.add_argument(
        "--resample-from",
        type=Timeframe,
        choices=list(Timeframe),
        help="Build the target from a smaller timeframe already stored locally",
    )
    args = parser.parse_args(argv)
    try:
        candle_window(args.start, args.end, args.timeframe)
        page_size = MAX_CANDLES
        if args.resample_from is not None:
            page_size //= resampling_ratio(args.resample_from, args.timeframe)
    except ValueError as exc:
        parser.error(str(exc))

    store = CandleStore(data_directory())
    try:
        store.check_ready()
        with httpx.Client(
            base_url=DELTA_URL, headers={"Accept": "application/json"}, timeout=15
        ) as client:
            if args.resample_from is None:
                provider = DeltaCandleProvider(client)
                provider.verify_market(BITCOIN)
                fetch = provider.fetch_candles
            else:
                fetch = partial(fetch_resampled, store, source=args.resample_from)
            report = backfill(
                store, BITCOIN, args.timeframe, args.start, args.end, fetch, page_size=page_size
            )
    except SQLAlchemyError as exc:
        print(
            f"Cannot access TradeOS storage. Run `uv run alembic upgrade head`. {exc}",
            file=sys.stderr,
        )
        return 1
    except (DeltaError, StorageError, ValueError) as exc:
        print(f"Backfill failed. {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
