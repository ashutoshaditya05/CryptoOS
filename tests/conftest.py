from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.market.storage import CandleStore


@pytest.fixture
def migrated_dir(tmp_path: Path) -> Path:
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.attributes["data_dir"] = tmp_path
    command.upgrade(config, "head")
    return tmp_path


@pytest.fixture
def store(migrated_dir: Path):
    storage = CandleStore(migrated_dir)
    try:
        yield storage
    finally:
        storage.close()
