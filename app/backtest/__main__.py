import argparse
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.backtest.models import BacktestRequest, ResearchSettings
from app.backtest.service import get_backtest, run_backtest
from app.config import data_directory
from app.market.storage import CandleStore, StorageError
from app.strategies.registry import STRATEGIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest long-only Bitcoin spot on stored candles."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--config", type=Path, help="JSON backtest request with custom assumptions")
    mode.add_argument("--get", help="Read a saved backtest by run ID")
    parser.add_argument("--start", help="First candle open, including timezone")
    parser.add_argument("--end", help="Last candle close, including timezone")
    parser.add_argument("--strategy-version", choices=STRATEGIES)
    parser.add_argument("--research", action="store_true", help="Include splits and sensitivity")
    parser.add_argument("--full", action="store_true", help="Include order ledger and equity curve")
    args = parser.parse_args(argv)
    if (args.config or args.get) and any(
        (args.start, args.end, args.strategy_version, args.research)
    ):
        parser.error("Use --config or --get separately from replay options.")
    if not (args.config or args.get or (args.start and args.end)):
        parser.error("Provide --start and --end, --config, or --get.")

    store = CandleStore(data_directory())
    try:
        if args.get:
            report = get_backtest(store, args.get)
            if report is None:
                parser.error(f"Backtest {args.get!r} was not found.")
        else:
            if args.config:
                request = BacktestRequest.model_validate_json(args.config.read_bytes())
            else:
                options = (
                    {"strategy_version": args.strategy_version} if args.strategy_version else {}
                )
                request = BacktestRequest(
                    start=args.start,
                    end=args.end,
                    research=ResearchSettings() if args.research else None,
                    **options,
                )
            report = run_backtest(store, request)
    except ValueError as exc:
        parser.error(str(exc))
    except (OSError, SQLAlchemyError, StorageError) as exc:
        print(f"Cannot read or save backtest. {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    exclude = None if args.full else {"result": {"orders", "equity_curve"}}
    print(report.model_dump_json(indent=2, exclude=exclude))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
