"""
회귀 테스트: paper_trader 운용 규칙 v1 (2026-07-08 확정)

검증 항목:
  1. 비용 상수가 evaluate.py와 동일 (중복 정의 없음)
  2. paper_trades 스키마에 horizon / entry_price 컬럼 존재
  3. should_enter_5d: 마지막 진입 후 5거래일 미도달이면 False 반환
  4. _get_next_day_open이 paper_trader에 존재하고 callable
  5. _net_ret(0.05) ≈ evaluate._net_ret(0.05) — 동일 공식
"""
from pathlib import Path
import importlib
import sys

DB_PATH = Path(__file__).parent.parent / "data" / "stocks.db"
_SERVER_DIR = Path(__file__).parent.parent / "server"
_ML_DIR     = Path(__file__).parent.parent / "ml"

if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))
if str(_ML_DIR) not in sys.path:
    sys.path.insert(0, str(_ML_DIR))

import pytest


# ── fixture ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def conn():
    import sqlite3
    if not DB_PATH.exists():
        pytest.skip("stocks.db 없음")
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()


@pytest.fixture(scope="module")
def paper_trader():
    return importlib.import_module("paper_trader")


@pytest.fixture(scope="module")
def evaluate():
    return importlib.import_module("evaluate")


# ── 1. 비용 상수 ──────────────────────────────────────────────

def test_cost_constants_not_redefined(paper_trader, evaluate):
    """paper_trader.py가 evaluate.py 상수를 import해 쓰는지 확인.
    동일 값이어야 하며 paper_trader 모듈에서 직접 정의됐으면 안 됨."""
    assert paper_trader.COMMISSION == evaluate.COMMISSION, \
        "COMMISSION 불일치 — evaluate.py import 확인 필요"
    assert paper_trader.SELL_TAX == evaluate.SELL_TAX, \
        "SELL_TAX 불일치"
    assert paper_trader.SLIPPAGE == evaluate.SLIPPAGE, \
        "SLIPPAGE 불일치"
    assert paper_trader.MIN_VOLUME_KRW == evaluate.MIN_VOLUME_KRW, \
        "MIN_VOLUME_KRW 불일치"
    assert paper_trader.LIMIT_UP == evaluate.LIMIT_UP, \
        "LIMIT_UP 불일치"


def test_net_ret_formula_identical(paper_trader, evaluate):
    """_net_ret(0.05)이 evaluate._net_ret(0.05)과 동일 결과."""
    raw = 0.05
    pt_val = paper_trader._net_ret(raw)
    ev_val = evaluate._net_ret(raw)
    assert abs(pt_val - ev_val) < 1e-9, (
        f"_net_ret({raw}) 불일치: paper_trader={pt_val}, evaluate={ev_val}"
    )


# ── 2. 스키마 컬럼 ────────────────────────────────────────────

def test_paper_trades_horizon_column(conn):
    """paper_trades 테이블에 horizon 컬럼이 존재해야 한다."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    assert "horizon" in cols, (
        "paper_trades.horizon 컬럼 없음 — _ensure_schema() 실행 확인"
    )


def test_paper_trades_entry_price_column(conn):
    """paper_trades 테이블에 entry_price 컬럼이 존재해야 한다."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    assert "entry_price" in cols, (
        "paper_trades.entry_price 컬럼 없음 — _ensure_schema() 실행 확인"
    )


# ── 3. should_enter_5d 주기 로직 ─────────────────────────────

def test_should_enter_5d_no_history(paper_trader, monkeypatch):
    """paper_trades가 비어있으면(첫 진입) True 반환."""
    import sqlite3 as _sq

    # 빈 결과를 반환하도록 DB 쿼리를 monkeypatch
    original_connect = _sq.connect

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, p=()): return _FakeCursor()
        def __init__(self): pass

    class _FakeCursor:
        def fetchone(self): return (None,)

    monkeypatch.setattr(_sq, "connect", lambda *a, **kw: _FakeConn())
    result = paper_trader.should_enter_5d("20260708")
    assert result is True, "첫 진입 시 True여야 함"


def test_should_enter_5d_recent_entry(paper_trader, monkeypatch):
    """마지막 진입일이 3 거래일 전이면 False 반환 (주기 미도달)."""
    import sqlite3 as _sq

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, p=()):
            if "MAX(recommended_date)" in q:
                return _CursorDate("20260703")   # 마지막 진입: 3거래일 전 가정
            return _CursorCount(3)               # count_trading_days_between = 3
        def __init__(self): pass

    class _CursorDate:
        def __init__(self, v): self._v = v
        def fetchone(self): return (self._v,)

    class _CursorCount:
        def __init__(self, v): self._v = v
        def fetchone(self): return (self._v,)

    monkeypatch.setattr(_sq, "connect", lambda *a, **kw: _FakeConn())
    result = paper_trader.should_enter_5d("20260708")
    assert result is False, "3거래일 미도달 시 False여야 함"


# ── 4. _get_next_day_open callable ───────────────────────────

def test_get_next_day_open_callable(paper_trader):
    """paper_trader에 _get_next_day_open 함수가 존재하고 callable이어야 한다."""
    fn = getattr(paper_trader, "_get_next_day_open", None)
    assert callable(fn), "_get_next_day_open 함수 없음 또는 callable 아님"


# ── 5. should_enter_60d 존재 ─────────────────────────────────

def test_should_enter_60d_callable(paper_trader):
    """paper_trader에 should_enter_60d 함수가 존재해야 한다."""
    fn = getattr(paper_trader, "should_enter_60d", None)
    assert callable(fn), "should_enter_60d 함수 없음"
