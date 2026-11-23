from pathlib import Path

from sqlalchemy import CheckConstraint, Column, Index, Integer, MetaData, String, Table, event
from sqlalchemy.engine import URL, Engine, create_engine

metadata = MetaData()

candle_batches = Table(
    "candle_batches",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("venue", String(64), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("first_open", Integer, nullable=False),
    Column("last_open", Integer, nullable=False),
    CheckConstraint("first_open <= last_open", name="ck_candle_batches_ordered_range"),
)

Index(
    "ix_candle_batches_lookup",
    candle_batches.c.venue,
    candle_batches.c.symbol,
    candle_batches.c.timeframe,
    candle_batches.c.first_open,
    candle_batches.c.last_open,
)


def create_storage_engine(data_dir: Path) -> Engine:
    url = URL.create("sqlite+pysqlite", database=str(data_dir / "tradeos.sqlite3"))
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record) -> None:
        connection.execute("PRAGMA busy_timeout = 10000")

    return engine
