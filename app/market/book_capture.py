import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.market.orderbook import SYMBOL, OrderBook

ENDPOINT = "wss://public-socket.india.delta.exchange"
CHANNELS = (
    ("ob_updates", SYMBOL),
    ("trades", SYMBOL),
    ("mark_price", f"MARK:{SYMBOL}"),
)


async def capture(path: Path, seconds: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    subscription = json.dumps(
        {
            "type": "subscribe",
            "payload": {
                "channels": [{"name": name, "symbols": [symbol]} for name, symbol in CHANNELS]
            },
        }
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    base_us = time.time_ns() // 1_000
    monotonic_start = time.monotonic_ns()
    count = connection = 0
    with path.open("x", encoding="utf-8", buffering=1) as stream:
        stream.write(json.dumps({"schema": 1, "venue": "delta_india", "symbol": SYMBOL}) + "\n")
        while loop.time() < deadline:
            connection += 1
            try:
                async with (
                    asyncio.timeout_at(deadline),
                    connect(
                        ENDPOINT, open_timeout=10, close_timeout=1, max_size=8_388_608
                    ) as socket,
                ):
                    await socket.send(subscription)
                    book = OrderBook()
                    async for frame in socket:
                        arrival = base_us + (time.monotonic_ns() - monotonic_start) // 1_000
                        message = json.loads(frame)
                        record = {
                            "arrival_us": arrival,
                            "connection": connection,
                            "message": message,
                        }
                        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                        count += 1
                        if message.get("type") == "ob_updates":
                            book.apply(message)
            except (OSError, WebSocketException, TimeoutError, ValueError) as exc:
                if loop.time() >= deadline:
                    break
                print(f"Delta connection {connection} ended: {exc}", file=sys.stderr)
                await asyncio.sleep(min(3, max(0, deadline - loop.time())))
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Record Delta India BTCUSD public market events.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("Recording duration must be positive.")
    try:
        count = asyncio.run(capture(args.output, args.seconds))
    except OSError as exc:
        parser.error(str(exc))
    print(f"Recorded {count} messages to {args.output}")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
