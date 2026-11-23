import os
from pathlib import Path


def data_directory() -> Path:
    return Path(os.environ.get("TRADEOS_DATA_DIR", "data")).expanduser().resolve()
