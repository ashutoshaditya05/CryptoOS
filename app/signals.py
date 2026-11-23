import argparse
import sys
from datetime import datetime

from pydantic import TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from app.config import data_directory
from app.market.storage import CandleStore, StorageError
from app.strategies.engine import generate_signals
from app.strategies.models import Position, PullbackSettings, Signal
from app.strategies.registry import DEFAULT_STRATEGY_VERSION, STRATEGIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Bitcoin signals from stored candles.")
    parser.add_argument(
        "--start", required=True, type=datetime.fromisoformat, help="First candle open"
    )
    parser.add_argument(
        "--end", required=True, type=datetime.fromisoformat, help="Last candle close"
    )
    parser.add_argument("--position", type=Position, choices=list(Position), default=Position.FLAT)
    parser.add_argument("--strategy-version", choices=STRATEGIES, default=DEFAULT_STRATEGY_VERSION)
    parser.add_argument("--min-adx", help="Minimum ADX for entry, default 20")
    parser.add_argument("--max-atr-fraction", help="Maximum ATR / close, default 0.03")
    parser.add_argument("--atr-stop-multiple", help="Stop distance in ATR, default 1.5")
    parser.add_argument("--reward-risk-ratio", help="Target / stop distance, default 2")
    args = parser.parse_args(argv)

    store = CandleStore(data_directory())
    try:
        settings = PullbackSettings.model_validate(
            {
                k: v
                for k, v in vars(args).items()
                if k in PullbackSettings.model_fields and v is not None
            }
        )
        signals = generate_signals(
            store,
            args.start,
            args.end,
            settings=settings,
            position=args.position,
            strategy_version=args.strategy_version,
        )
    except ValueError as exc:
        parser.error(str(exc))
    except (SQLAlchemyError, StorageError) as exc:
        print(f"Cannot read TradeOS candles. {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(TypeAdapter(list[Signal]).dump_json(signals, indent=2).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
