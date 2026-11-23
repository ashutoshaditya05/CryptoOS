import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.database import candle_batches, create_storage_engine
from app.market.models import DECIMAL_PRECISION, DECIMAL_SCALE, Candle, Market, Timeframe

DECIMAL_TYPE = pa.decimal256(DECIMAL_PRECISION, DECIMAL_SCALE)
UTC_TIMESTAMP = pa.timestamp("s", tz="+00:00")
CANDLE_SCHEMA = pa.schema(
    [
        pa.field("venue", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("open_time", UTC_TIMESTAMP, nullable=False),
        pa.field("close_time", UTC_TIMESTAMP, nullable=False),
        pa.field("open", DECIMAL_TYPE, nullable=False),
        pa.field("high", DECIMAL_TYPE, nullable=False),
        pa.field("low", DECIMAL_TYPE, nullable=False),
        pa.field("close", DECIMAL_TYPE, nullable=False),
        pa.field("volume", DECIMAL_TYPE),
        pa.field("is_closed", pa.bool_(), nullable=False),
    ]
)


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredPage:
    inserted: int
    duplicates: int
    open_times: list[datetime]


class CandleStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "candles"
        self.engine = create_storage_engine(data_dir)

    def close(self) -> None:
        self.engine.dispose()

    def check_ready(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(select(candle_batches.c.id).limit(1)).first()

    def read(
        self, market: Market, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        with self.engine.connect() as connection:
            return self._read_window(connection, market, timeframe, start, end)

    def write_page(
        self,
        market: Market,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        candles: list[Candle],
    ) -> StoredPage:
        incoming: dict[datetime, Candle] = {}
        for candle in candles:
            if (
                (candle.venue, candle.symbol, candle.timeframe)
                != (market.venue, market.symbol, timeframe)
                or not start <= candle.open_time < end
                or candle.close_time > end
                or not candle.is_closed
            ):
                raise StorageError(
                    "Only closed candles from the selected series and page can write."
                )
            previous = incoming.get(candle.open_time)
            if previous is not None and previous != candle:
                raise StorageError(
                    f"Conflicting candles in one page at {candle.open_time.isoformat()}."
                )
            incoming[candle.open_time] = candle

        with self.engine.begin() as connection:
            # A single SQLite writer checks for duplicates and publishes each immutable batch.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            existing = {
                candle.open_time: candle
                for candle in self._read_window(connection, market, timeframe, start, end)
            }
            new = []
            for open_time, candle in sorted(incoming.items()):
                previous = existing.get(open_time)
                if previous is None:
                    new.append(candle)
                elif previous != candle:
                    raise StorageError(
                        f"Stored candle conflicts with new data at {open_time.isoformat()}."
                    )

            if new:
                batch_id = uuid4().hex
                self._write_file(self._file_path(timeframe, batch_id), new)
                connection.execute(
                    candle_batches.insert().values(
                        id=batch_id,
                        venue=market.venue,
                        symbol=market.symbol,
                        timeframe=timeframe.value,
                        first_open=int(new[0].open_time.timestamp()),
                        last_open=int(new[-1].open_time.timestamp()),
                    )
                )

        return StoredPage(
            len(new), len(candles) - len(new), sorted(existing.keys() | incoming.keys())
        )

    def _file_path(self, timeframe: Timeframe, batch_id: str) -> Path:
        return self.root / timeframe.value / f"{batch_id}.parquet"

    def _read_window(
        self,
        connection: Connection,
        market: Market,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        batch_ids = connection.scalars(
            select(candle_batches.c.id).where(
                candle_batches.c.venue == market.venue,
                candle_batches.c.symbol == market.symbol,
                candle_batches.c.timeframe == timeframe.value,
                candle_batches.c.first_open < int(end.timestamp()),
                candle_batches.c.last_open >= int(start.timestamp()),
            )
        ).all()
        candles = []
        for batch_id in batch_ids:
            path = self._file_path(timeframe, batch_id)
            try:
                with pq.ParquetFile(path) as parquet:
                    rows = parquet.read().to_pylist()
                candles.extend(
                    Candle.model_validate(row) for row in rows if start <= row["open_time"] < end
                )
            except (OSError, pa.ArrowException, ValidationError) as exc:
                raise StorageError(f"Cannot read candle batch {batch_id}: {exc}") from exc
        return sorted(candles, key=lambda candle: candle.open_time)

    @staticmethod
    def _write_file(path: Path, candles: list[Candle]) -> None:
        temporary = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pylist(
                [candle.model_dump() for candle in candles], schema=CANDLE_SCHEMA
            )
            with temporary.open("xb") as file:
                pq.write_table(table, file, compression="zstd")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            if os.name == "posix":
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except (OSError, pa.ArrowException) as exc:
            raise StorageError(f"Cannot write candle batch {path.name}: {exc}") from exc
        finally:
            if temporary.exists():
                temporary.unlink()
