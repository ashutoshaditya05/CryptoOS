from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy.exc import SQLAlchemyError

from app.config import data_directory
from app.market.models import BITCOIN, Candle, Market, Timeframe, candle_window
from app.market.storage import CandleStore, StorageError
from app.strategies.engine import generate_signals
from app.strategies.models import Position, PullbackSettings, Signal
from app.strategies.registry import DEFAULT_STRATEGY_VERSION

MAX_QUERY_CANDLES = 2_000


class SignalQuery(PullbackSettings):
    start: AwareDatetime
    end: AwareDatetime
    position: Position = Position.FLAT
    strategy_version: str = DEFAULT_STRATEGY_VERSION


def create_app(data_dir: Path | None = None) -> FastAPI:
    store = CandleStore(data_dir if data_dir is not None else data_directory())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="TradeOS", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        try:
            store.check_ready()
        except SQLAlchemyError as exc:
            raise HTTPException(
                503, "Storage unavailable. Check the data directory and migration."
            ) from exc
        return {"status": "ok"}

    @app.get("/markets")
    def markets() -> list[Market]:
        return [BITCOIN]

    @app.get("/candles")
    def candles(
        start: AwareDatetime,
        end: AwareDatetime,
        timeframe: Timeframe = Timeframe.FIVE_MINUTES,
    ) -> list[Candle]:
        try:
            start, end = candle_window(start, end, timeframe)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if (end - start) // timeframe.duration > MAX_QUERY_CANDLES:
            raise HTTPException(422, f"Limit each query to {MAX_QUERY_CANDLES} candle slots.")
        try:
            return store.read(BITCOIN, timeframe, start, end)
        except (SQLAlchemyError, StorageError) as exc:
            raise HTTPException(503, "Candle storage unavailable.") from exc

    @app.get("/signals")
    def signals(query: Annotated[SignalQuery, Query()]) -> list[Signal]:
        try:
            return generate_signals(
                store,
                query.start,
                query.end,
                settings=query,
                position=query.position,
                strategy_version=query.strategy_version,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (SQLAlchemyError, StorageError) as exc:
            raise HTTPException(503, "Candle storage unavailable.") from exc

    return app


app = create_app()
