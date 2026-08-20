"""
회귀 테스트: daily_pipeline.py 스케줄러 가드

1. --force 플래그 존재 확인
2. _write_skip_status() 가 올바른 JSON을 기록하는지 확인
3. _is_already_current() 가 오늘 날짜 prices 를 인식하는지 확인
4. 주말 가드 로직이 소스에 존재하는지 확인
5. already_current 가드 로직이 소스에 존재하는지 확인
6. POST /api/update 가 --force 를 쓰는지 확인
"""
import json
import sqlite3
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).parent.parent
PIPELINE = BACKEND / "daily_pipeline.py"
MAIN_PY  = BACKEND / "server" / "main.py"


def _read_pipeline_src() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def _read_main_src() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


# ── 1. --force 플래그 ─────────────────────────────────────────────────────────

def test_force_flag_in_argparse():
    """--force 인수가 argparse에 선언돼 있어야 한다."""
    src = _read_pipeline_src()
    assert '"--force"' in src or "'--force'" in src, \
        "--force 인수가 daily_pipeline.py argparse에 없음"


def test_force_weekend_kept_for_compat():
    """--force-weekend 가 하위호환으로 여전히 존재해야 한다."""
    src = _read_pipeline_src()
    assert "--force-weekend" in src, "--force-weekend 가 제거됐음 (하위호환 필요)"


# ── 2. _write_skip_status ─────────────────────────────────────────────────────

def test_write_skip_status_writes_json():
    """_write_skip_status('weekend') 가 올바른 JSON 을 파일에 기록해야 한다."""
    sys.path.insert(0, str(BACKEND))
    import importlib
    import daily_pipeline as dp

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)

    original = dp.STATUS_FILE
    try:
        dp.STATUS_FILE = tmp
        dp._write_skip_status("weekend")
        data = json.loads(tmp.read_text(encoding="utf-8"))
        assert data["status"] == "skipped"
        assert data["reason"] == "weekend"
        assert "last_heartbeat" in data
    finally:
        dp.STATUS_FILE = original
        tmp.unlink(missing_ok=True)


def test_write_skip_status_already_current():
    """_write_skip_status('already_current') 가 reason 을 올바르게 기록해야 한다."""
    sys.path.insert(0, str(BACKEND))
    import daily_pipeline as dp

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)

    original = dp.STATUS_FILE
    try:
        dp.STATUS_FILE = tmp
        dp._write_skip_status("already_current")
        data = json.loads(tmp.read_text(encoding="utf-8"))
        assert data["status"] == "skipped"
        assert data["reason"] == "already_current"
    finally:
        dp.STATUS_FILE = original
        tmp.unlink(missing_ok=True)


# ── 3. _is_already_current ────────────────────────────────────────────────────
#
# 새 로직 (두-쿼리):
#   1. prices MAX(date) 조회
#   2. market_index MAX(date) WHERE date < today → 직전 거래일
#   규칙:
#     a. 직전 거래일 데이터 없음            → 실행(False)
#     b. 16:30 이전 AND 직전 거래일 최신   → 스킵(True)
#     c. 16:30 이전 AND 직전 거래일 stale  → 실행(False)
#     d. 16:30 이후 AND 오늘 데이터 있음   → 스킵(True)
#     e. 16:30 이후 AND 오늘 데이터 없음   → 실행(False)


def _make_fake_conn(prices_date, prev_trading_date):
    """두 쿼리를 SQL 문자열로 구분하는 목 커넥션을 반환한다."""
    class _FC:
        def __init__(self):
            self._last_sql = ""

        def execute(self, sql, params=None):
            self._last_sql = sql
            return self

        def fetchone(self):
            if "market_index" in self._last_sql:
                return (prev_trading_date,)
            return (prices_date,)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def close(self):
            pass

    return _FC()


def test_is_already_current_returns_true_for_today():
    """16:30 이후, prices가 오늘, 직전 거래일도 있음 → True(스킵).

    새 두-쿼리 방식으로 mock.
    """
    sys.path.insert(0, str(BACKEND))
    import daily_pipeline as dp

    today = date.today()
    today_str = today.strftime("%Y%m%d")
    prev_str = "20250101"  # 직전 거래일(오늘보다 이전이면 충분)
    fake_now = datetime.combine(today, time(17, 0))

    with patch("daily_pipeline.sqlite3") as mock_sqlite:
        mock_sqlite.connect.return_value = _make_fake_conn(
            prices_date=today_str, prev_trading_date=prev_str
        )
        result = dp._is_already_current(_now=fake_now)

    assert result is True, "오늘 데이터 있고 16:30 이후인데 False 반환"


def test_is_already_current_returns_false_after_1630_stale():
    """16:30 이후, prices가 직전 거래일보다 stale → False(실행 필요)."""
    sys.path.insert(0, str(BACKEND))
    import daily_pipeline as dp

    today = date.today()
    today_str = today.strftime("%Y%m%d")
    prev_str = "20250801"   # 직전 거래일
    old_prices = "20250731"  # prices는 그보다 오래된 날짜
    fake_now = datetime.combine(today, time(17, 0))

    with patch("daily_pipeline.sqlite3") as mock_sqlite:
        mock_sqlite.connect.return_value = _make_fake_conn(
            prices_date=old_prices, prev_trading_date=prev_str
        )
        result = dp._is_already_current(_now=fake_now)

    assert result is False, "stale prices인데 True(스킵) 반환"


def test_is_already_current_skips_before_1630_with_prev_data():
    """16:30 이전이고 직전 거래일 데이터가 최신 → True(스킵, 장 중이라 새 데이터 없음)."""
    sys.path.insert(0, str(BACKEND))
    import daily_pipeline as dp

    today = date.today()
    prev_str = "20250801"  # 직전 거래일
    fake_now = datetime.combine(today, time(13, 52))  # 장 중

    with patch("daily_pipeline.sqlite3") as mock_sqlite:
        mock_sqlite.connect.return_value = _make_fake_conn(
            prices_date=prev_str, prev_trading_date=prev_str
        )
        result = dp._is_already_current(_now=fake_now)

    assert result is True, "장 중(13:52)이고 직전 거래일 데이터 최신인데 False 반환"


def test_is_already_current_runs_before_1630_when_stale():
    """16:30 이전이지만 직전 거래일 데이터도 없음 → False(밀린 데이터 따라잡기)."""
    sys.path.insert(0, str(BACKEND))
    import daily_pipeline as dp

    today = date.today()
    old_prices = "20250720"  # 매우 오래된 prices
    prev_str   = "20250801"  # 직전 거래일은 그보다 최신
    fake_now = datetime.combine(today, time(13, 52))

    with patch("daily_pipeline.sqlite3") as mock_sqlite:
        mock_sqlite.connect.return_value = _make_fake_conn(
            prices_date=old_prices, prev_trading_date=prev_str
        )
        result = dp._is_already_current(_now=fake_now)

    assert result is False, "밀린 데이터 있는데 True(스킵) 반환"


def test_is_already_current_returns_false_on_db_error():
    """DB 접근 실패 시 False를 반환해야 한다 (안전 방향)."""
    sys.path.insert(0, str(BACKEND))
    import daily_pipeline as dp

    with patch("daily_pipeline.sqlite3") as mock_sqlite:
        mock_sqlite.connect.side_effect = OSError("DB 없음")
        result = dp._is_already_current(_now=datetime.now())

    assert result is False, "DB 오류인데 True 반환"


# ── 4/5. 소스 패턴 — 가드 로직 존재 확인 ─────────────────────────────────────

def test_weekend_guard_writes_skip_status():
    """주말 가드가 _write_skip_status('weekend') 를 호출해야 한다."""
    src = _read_pipeline_src()
    assert "_write_skip_status" in src, "_write_skip_status 함수가 없음"
    assert '"weekend"' in src or "'weekend'" in src, \
        "주말 스킵 시 reason='weekend' 가 없음"


def test_already_current_guard_in_source():
    """이미최신 가드가 소스에 존재해야 한다."""
    src = _read_pipeline_src()
    assert "_is_already_current" in src, "_is_already_current 함수가 없음"
    assert '"already_current"' in src or "'already_current'" in src, \
        "already_current reason 이 없음"


def test_both_guards_before_acquire_lock():
    """두 가드 모두 acquire_lock() 호출 전에 위치해야 한다."""
    src = _read_pipeline_src()
    skip_pos = src.find("_write_skip_status")
    lock_pos = src.find("acquire_lock()")
    assert skip_pos < lock_pos, \
        "_write_skip_status 가 acquire_lock() 이후에 위치함 — 가드 순서 잘못됨"


# ── 6. POST /api/update 가 --force 를 사용하는지 ──────────────────────────────

def test_api_update_uses_force_flag():
    """POST /api/update 실행 cmd 에 --force 가 포함돼야 한다."""
    src = _read_main_src()
    # --force-weekend 는 제거되고 --force 가 있어야 함
    assert '"--force"' in src or "'--force'" in src, \
        "main.py의 /api/update cmd에 --force 가 없음"


def test_api_update_not_using_force_weekend_only():
    """main.py /api/update cmd 에 --force-weekend 단독 사용이 없어야 한다.

    --force-weekend 가 있더라도 --force 도 함께 있으면 OK (하위호환 시나리오).
    완전히 --force-weekend 만 있는 경우를 차단.
    """
    src = _read_main_src()
    # start_update 함수 안에서 cmd 정의 라인을 찾음
    import re
    # cmd = [...] 블록에서 --force-weekend 단독 사용 여부 확인
    cmd_line_match = re.search(r'cmd\s*=\s*\[.*?--force.*?\]', src, re.DOTALL)
    if cmd_line_match:
        cmd_str = cmd_line_match.group()
        if "--force-weekend" in cmd_str and "--force\"" not in cmd_str and "--force'" not in cmd_str:
            pytest.fail(
                "start_update cmd 에 --force-weekend 만 있고 --force 가 없음 — "
                "--force 로 교체해야 함"
            )
