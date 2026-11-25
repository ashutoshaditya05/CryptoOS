import argparse
import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path

from app.backtest.models import ReplaySettings
from app.backtest.research import research
from app.market.book_tape import load_tape


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Cannot encode {type(value).__name__}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay Delta BTCUSD order-book imbalance.")
    parser.add_argument("--input", required=True, type=Path, help="Recorded BTCUSD JSONL tape")
    parser.add_argument("--settings", type=Path, help="Optional JSON replay settings")
    parser.add_argument("--output", type=Path, help="Write the research report to this file")
    parser.add_argument("--ledger", action="store_true", help="Include trade and signal details")
    args = parser.parse_args()
    try:
        settings = (
            ReplaySettings.model_validate_json(args.settings.read_bytes())
            if args.settings
            else ReplaySettings()
        )
        tape = load_tape(args.input)
        report = json.dumps(research(tape, settings, ledger=args.ledger), default=encode, indent=2)
        if args.output:
            if args.output.resolve() in {
                path.resolve() for path in (args.input, args.settings) if path is not None
            }:
                raise ValueError("The report must have a separate output path.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report + "\n", encoding="utf-8")
            print(f"Saved report to {args.output}")
        else:
            print(report)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
