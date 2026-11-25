from datetime import UTC, datetime
from decimal import Decimal
from time import sleep
from typing import Any

import httpx

from app.market.models import BITCOIN, Candle, Market, Timeframe

DELTA_URL = "https://api.india.delta.exchange"
MAX_CANDLES = 2_000


class DeltaError(RuntimeError):
    pass


class DeltaCandleProvider:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def verify_market(self, market: Market) -> None:
        if market != BITCOIN:
            raise DeltaError("Only Delta India BTC_INR spot is configured.")
        result = self._get(f"/v2/products/{market.symbol}")
        if (
            not isinstance(result, dict)
            or result.get("symbol") != market.symbol
            or result.get("contract_type") != "spot"
        ):
            raise DeltaError(f"Delta did not identify {market.symbol} as a spot product.")

    def fetch_candles(
        self, market: Market, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        if market != BITCOIN:
            raise DeltaError("Only Delta India BTC_INR spot is configured.")
        candles = []
        for row in self.history(market.symbol, timeframe, start, end):
            try:
                if not isinstance(row, dict) or type(row.get("time")) is not int:
                    raise ValueError("Expected an integer Unix timestamp in seconds.")
                open_time = datetime.fromtimestamp(row["time"], tz=UTC)
                if not start <= open_time < end:
                    continue
                candles.append(
                    Candle(
                        venue=market.venue,
                        symbol=market.symbol,
                        timeframe=timeframe,
                        open_time=open_time,
                        close_time=open_time + timeframe.duration,
                        open=row["open"],
                        high=row["high"],
                        low=row["low"],
                        close=row["close"],
                        volume=row.get("volume"),
                        is_closed=True,
                    )
                )
            except (KeyError, ValueError, OverflowError, OSError) as exc:
                raise DeltaError(f"Delta returned an invalid candle: {exc}") from exc
        return candles

    def history(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        result = self._get(
            "/v2/history/candles",
            {
                "symbol": symbol,
                "resolution": timeframe.value,
                "start": int(start.timestamp()),
                "end": int(end.timestamp()) - 1,
            },
        )
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise DeltaError("Delta returned an invalid candle list.")
        return result

    def _get(self, path: str, params: dict[str, str | int] | None = None) -> Any:
        for attempt in range(3):
            try:
                response = self.client.get(path, params=params)
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise DeltaError(f"Delta request failed for {path}: {exc}") from exc
                sleep(2**attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = (
                        int(retry_after) if retry_after.isascii() and retry_after.isdigit() else 0
                    )
                    if delay > 30:
                        raise DeltaError(f"Delta asked to retry {path} in {delay} seconds.")
                    sleep(max(2**attempt, delay))
                    continue
            if not response.is_success:
                raise DeltaError(f"Delta returned HTTP {response.status_code} for {path}.")

            try:
                payload = response.json(parse_float=Decimal)
            except ValueError as exc:
                raise DeltaError(f"Delta returned invalid JSON for {path}.") from exc
            if not isinstance(payload, dict) or payload.get("success") is not True:
                error = payload.get("error") if isinstance(payload, dict) else None
                code = error.get("code") if isinstance(error, dict) else None
                raise DeltaError(f"Delta returned an unsuccessful response for {path}: {code!r}.")
            if "result" not in payload:
                raise DeltaError(f"Delta returned no result for {path}.")
            return payload["result"]

        raise AssertionError("Delta retry loop exhausted without a result or error.")
