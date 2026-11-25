from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from heapq import nlargest, nsmallest
from zlib import crc32

SYMBOL = "BTCUSD"
QUOTE_DEPTH = 50
type Level = tuple[Decimal, int]
type RawLevel = tuple[str, str, int]


class BookError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Quote:
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @property
    def bid(self) -> Decimal:
        return self.bids[0][0]

    @property
    def ask(self) -> Decimal:
        return self.asks[0][0]

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) * 10_000 / self.mid

    def imbalance(self, levels: int) -> Decimal | None:
        if min(len(self.bids), len(self.asks)) < levels:
            return None
        bids = sum(size for _, size in self.bids[:levels])
        asks = sum(size for _, size in self.asks[:levels])
        return Decimal(bids - asks) / (bids + asks)


class OrderBook:
    def __init__(self) -> None:
        self.sequence: int | None = None
        self.bids: dict[Decimal, RawLevel] = {}
        self.asks: dict[Decimal, RawLevel] = {}

    def apply(self, message: dict) -> Quote:
        action = message.get("action")
        sequence = message.get("seq")
        if type(sequence) is not int or action not in ("snapshot", "update"):
            raise BookError("Missing sequence or invalid book action.")
        if action == "snapshot":
            self.bids.clear()
            self.asks.clear()
        elif self.sequence is None or sequence != self.sequence + 1:
            raise BookError("Nonconsecutive depth sequence.")

        try:
            for side, name in ((self.bids, "b"), (self.asks, "a")):
                for price_text, size_text in message[name]:
                    price, size = Decimal(price_text), Decimal(size_text)
                    if (
                        not price.is_finite()
                        or price <= 0
                        or not size.is_finite()
                        or size < 0
                        or size != size.to_integral_value()
                    ):
                        raise BookError("Invalid depth price or contract size.")
                    if size == 0:
                        side.pop(price, None)
                    else:
                        side[price] = (price_text, size_text, int(size))
        except (InvalidOperation, KeyError, TypeError) as exc:
            raise BookError("Invalid depth levels.") from exc

        bids = nlargest(QUOTE_DEPTH, self.bids)
        asks = nsmallest(QUOTE_DEPTH, self.asks)
        checksum = "|".join(
            ",".join(f"{side[price][0]}:{side[price][1]}" for price in prices[:10])
            for side, prices in ((self.asks, asks), (self.bids, bids))
        )
        if crc32(checksum.encode()) != message.get("cs"):
            raise BookError("Order-book checksum mismatch.")
        if not bids or not asks or bids[0] >= asks[0]:
            raise BookError("Missing or crossed quote.")
        self.sequence = sequence
        return Quote(
            tuple((price, self.bids[price][2]) for price in bids),
            tuple((price, self.asks[price][2]) for price in asks),
        )
