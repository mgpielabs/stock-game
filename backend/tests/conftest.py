"""pytest 공통 fixtures."""
import sys
from pathlib import Path

import pytest

# 백엔드 모듈 경로 등록
BACKEND = Path(__file__).parent.parent
DATA_DIR = BACKEND / "data"
ML_DIR   = BACKEND / "ml"
FEAT_DIR = BACKEND / "features"
SRV_DIR  = BACKEND / "server"

for p in [str(DATA_DIR), str(ML_DIR), str(FEAT_DIR), str(SRV_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

DB_PATH = DATA_DIR / "stocks.db"


@pytest.fixture(scope="session")
def db_path():
    return DB_PATH


@pytest.fixture(scope="session")
def conn(db_path):
    import sqlite3
    c = sqlite3.connect(db_path, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()
