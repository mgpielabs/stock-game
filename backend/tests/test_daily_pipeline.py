"""
회귀 테스트: daily_pipeline._is_already_current() 가드 로직

수정 사항 (2026-08-10):
  prices_date >= today → prices_date == today  (>= 의 잠재 버그 수정)
  16:30 이전 실행은 항상 통과  (StartWhenAvailable 조기 실행 대응)
  _now 파라미터로 datetime.now() 주입 가능 (테스트 전용)
"""
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# backend/ 루트를 sys.path에 추가해 daily_pipeline import
sys.path.insert(0, str(Path(__file__).parent.parent))
import daily_pipeline as dp


def _mock_db(prices_date_str, prev_trading_date=None):
    """두 쿼리(prices / market_index)를 SQL 문자열로 구분하는 목 커넥션.

    prev_trading_date 를 생략하면 prices_date_str 와 동일한 값으로 설정된다
    (대부분의 기존 테스트는 이 동작에 의존 — 레거시 호환).
    """
    if prev_trading_date is None:
        prev_trading_date = prices_date_str

    class _FC:
        def __init__(self):
            self._last_sql = ""
        def execute(self, sql, params=None):
            self._last_sql = sql
            return self
        def fetchone(self):
            if "market_index" in self._last_sql:
                return (prev_trading_date,)
            return (prices_date_str,)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def close(self):
            pass

    return _FC()


# ── 핵심 버그 시나리오 ────────────────────────────────────────────────────────

def test_friday_data_monday_18h_should_run():
    """금요일 데이터만 있을 때 월요일 18시에 실행하면 스킵하지 않아야 한다.

    이전 >= 조건에서도 문자열 비교("20260808" >= "20260811" → False)로 동작은 같았으나,
    == 조건으로 의도를 명확히 하고 미래 날짜 등 edge case를 막는다.
    """
    monday_18h = datetime(2026, 8, 11, 18, 0)   # 월요일 18:00
    friday_date = "20260808"                      # 금요일 prices 데이터
    with patch("daily_pipeline.sqlite3.connect", return_value=_mock_db(friday_date)):
        result = dp._is_already_current(_now=monday_18h)
    assert result is False, "금요일 데이터가 있어도 월요일은 스킵하면 안 됨"


def test_monday_data_monday_18h_should_skip():
    """오늘(월요일) 데이터가 이미 있고 18시이면 스킵해야 한다 (중복 실행 방지)."""
    monday_18h = datetime(2026, 8, 11, 18, 0)
    monday_date = "20260811"
    with patch("daily_pipeline.sqlite3.connect", return_value=_mock_db(monday_date)):
        result = dp._is_already_current(_now=monday_18h)
    assert result is True, "오늘 데이터가 이미 있으면 스킵해야 함"


# ── 시각 조건 ─────────────────────────────────────────────────────────────────

def test_before_1630_skips_when_prev_data_current():
    """16:30 이전이고 직전 거래일 데이터가 이미 있으면 스킵해야 한다.

    장 중이라 새 데이터가 없으므로 불필요한 재수집을 막는다.
    (구 로직은 "16:30 이전이면 무조건 실행"이었으나 StartWhenAvailable 조기 기동 시
     어제 데이터를 또 수집하는 낭비가 발생해 수정됨.)
    """
    monday_9h = datetime(2026, 8, 11, 9, 0)    # 장 중 (월요일 9시)
    friday_date = "20260808"                     # 직전 거래일(금요일)
    # prices = 금요일 데이터, market_index 직전 거래일 = 금요일 → 최신
    with patch("daily_pipeline.sqlite3.connect",
               return_value=_mock_db(friday_date, friday_date)):
        result = dp._is_already_current(_now=monday_9h)
    assert result is True, "장 중 + 직전 거래일 데이터 최신 → 스킵해야 함"


def test_before_1630_runs_when_stale():
    """16:30 이전이라도 직전 거래일 데이터가 없으면 실행해야 한다 (밀린 데이터 따라잡기)."""
    monday_1629 = datetime(2026, 8, 11, 16, 29)
    old_prices = "20260731"    # 오래된 prices
    friday_date = "20260808"   # 직전 거래일은 그보다 최신
    with patch("daily_pipeline.sqlite3.connect",
               return_value=_mock_db(old_prices, friday_date)):
        result = dp._is_already_current(_now=monday_1629)
    assert result is False, "16:29라도 직전 거래일 데이터 없으면 실행해야 함"


def test_exactly_1630_should_skip_if_data_exists():
    """16:30 정각이고 오늘 데이터가 있으면 스킵한다."""
    monday_1630 = datetime(2026, 8, 11, 16, 30)
    monday_date = "20260811"
    with patch("daily_pipeline.sqlite3.connect", return_value=_mock_db(monday_date)):
        result = dp._is_already_current(_now=monday_1630)
    assert result is True, "16:30 정각 + 오늘 데이터 있음 → 스킵해야 함"


def test_exactly_1700_should_skip_if_data_exists():
    """17:00이고 오늘 데이터가 있으면 스킵한다."""
    monday_1700 = datetime(2026, 8, 11, 17, 0)
    monday_date = "20260811"
    with patch("daily_pipeline.sqlite3.connect", return_value=_mock_db(monday_date)):
        result = dp._is_already_current(_now=monday_1700)
    assert result is True


# ── 엣지 케이스 ───────────────────────────────────────────────────────────────

def test_no_prices_data_should_not_skip():
    """prices 테이블이 비어 있으면 (MAX(date)=None) 스킵하지 않는다."""
    monday_18h = datetime(2026, 8, 11, 18, 0)
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.return_value.fetchone.return_value = (None,)
    with patch("daily_pipeline.sqlite3.connect", return_value=conn):
        result = dp._is_already_current(_now=monday_18h)
    assert result is False, "prices 데이터가 없으면 스킵하면 안 됨"


def test_db_error_should_not_skip():
    """DB 접근 오류 시 스킵하지 않는다 (안전 방향)."""
    monday_18h = datetime(2026, 8, 11, 18, 0)
    with patch("daily_pipeline.sqlite3.connect", side_effect=Exception("DB error")):
        result = dp._is_already_current(_now=monday_18h)
    assert result is False, "DB 오류 시에는 안전하게 실행 방향으로 False 반환"


def test_future_prices_date_should_not_skip():
    """prices에 미래 날짜가 잘못 기록된 경우 스킵하지 않아야 한다.

    이전 >= 조건에서는 prices_date > today 이면 잘못 스킵될 수 있었음.
    == 조건으로 변경 후 오늘 날짜와 정확히 일치할 때만 스킵.
    """
    monday_18h = datetime(2026, 8, 11, 18, 0)
    future_date = "20261231"   # 미래 날짜 (잘못된 데이터)
    with patch("daily_pipeline.sqlite3.connect", return_value=_mock_db(future_date)):
        result = dp._is_already_current(_now=monday_18h)
    assert result is False, "미래 날짜 데이터가 있어도 오늘이 아니면 스킵하면 안 됨"
