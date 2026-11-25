import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.market.orderbook import SYMBOL, BookError, OrderBook, Quote


@dataclass(frozen=True, slots=True)
class Frame:
    time_us: int
    segment: int
    quote: Quote | None
    quote_us: int
    mark: Decimal | None
    mark_us: int
    signed_contracts: int = 0

    def has_quote(self, max_age_us: int) -> bool:
        return self.quote is not None and self.time_us - self.quote_us <= max_age_us


@dataclass(frozen=True)
class Tape:
    frames: list[Frame]
    quality: dict[str, int]
    sha256: str


def signed_size(message: dict) -> int:
    side = message.get("r")
    size = Decimal(str(message.get("s")))
    if side not in ("m", "t") or not size.is_finite() or size <= 0 or size % 1:
        raise ValueError("Invalid taker trade.")
    # The venue reports the buyer's role. A maker buyer implies a taker sell.
    return int(size) if side == "t" else -int(size)


def load_tape(path: Path) -> Tape:
    frames: list[Frame] = []
    quality: Counter[str] = Counter()
    digest = hashlib.sha256()
    connection = previous_time = segment = quote_us = mark_us = 0
    quote = mark = None
    book = OrderBook()

    with path.open("rb") as stream:
        header = stream.readline()
        if json.loads(header) != {"schema": 1, "venue": "delta_india", "symbol": SYMBOL}:
            raise ValueError(f"{path} is not a Delta India BTCUSD capture.")
        digest.update(header)

        for line_number, raw in enumerate(stream, start=2):
            try:
                row = json.loads(raw)
                timestamp, source, message = row["arrival_us"], row["connection"], row["message"]
                if (
                    type(timestamp) is not int
                    or timestamp < previous_time
                    or type(source) is not int
                    or source < max(1, connection)
                    or not isinstance(message, dict)
                ):
                    raise ValueError("Invalid capture order or envelope.")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid capture line {line_number} in {path}.") from exc

            digest.update(raw)
            previous_time = timestamp
            if source != connection:
                connection = source
                quality["connections"] += 1
                segment += 1
                quote = mark = None
                book = OrderBook()
            signed = 0
            match message.get("type"), message.get("sy", message.get("symbol")):
                case "ob_updates", name if name == SYMBOL:
                    if message.get("action") == "snapshot" and quote is not None:
                        segment += 1
                    try:
                        quote = book.apply(message)
                    except BookError as exc:
                        if quote is not None:
                            segment += 1
                        quote = None
                        book = OrderBook()
                        quality[f"book_error: {exc}"] += 1
                    else:
                        quote_us = timestamp
                        quality[f"book_{message['action']}s"] += 1
                case "trades", name if name == SYMBOL:
                    try:
                        signed = signed_size(message)
                    except (InvalidOperation, ValueError) as exc:
                        raise ValueError(f"Invalid trade on line {line_number}.") from exc
                    quality["trade_prints"] += 1
                case "mark_price", name if name == f"MARK:{SYMBOL}":
                    try:
                        mark = Decimal(message["p"])
                        if not mark.is_finite() or mark <= 0:
                            raise ValueError("Invalid mark price.")
                    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
                        raise ValueError(f"Invalid mark on line {line_number}.") from exc
                    mark_us = timestamp
                    quality["mark_prices"] += 1
                case _:
                    continue

            frames.append(Frame(timestamp, segment, quote, quote_us, mark, mark_us, signed))

    if not frames or not quality["book_snapshots"] or not quality["trade_prints"]:
        raise ValueError("The capture requires a verified book snapshot and signed trades.")
    return Tape(frames, dict(quality), digest.hexdigest())
