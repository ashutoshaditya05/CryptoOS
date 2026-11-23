from alembic import context

from app.config import data_directory
from app.database import create_storage_engine, metadata


def run_migrations() -> None:
    if context.is_offline_mode():
        context.configure(dialect_name="sqlite", target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    data_dir = context.config.attributes.get("data_dir", data_directory())
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_storage_engine(data_dir)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode = WAL")
            connection.commit()
            context.configure(connection=connection, target_metadata=metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
