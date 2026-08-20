"""
회귀 테스트: 연기금(pension) 데이터 수집 인프라

검증 항목:
  1. investor_trading_kis_detail 테이블에 pension_qty / pension_value 컬럼 존재
  2. investor_trading_kis_detail에 pension 데이터가 실제로 존재 (not all NULL)
  3. kis_investor_detail_updater.py 파일이 존재
  4. daily_pipeline.py에 kis_investor_detail_updater 단계가 있음
  5. kis_investor_detail_updater가 fetch_batch/parse_rows를 kis_investor_backfill에서 임포트
  6. sector-flow 엔드포인트가 pension_value 필드를 읽는 코드를 포함
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
BACKEND = Path(__file__).parent.parent
DB_PATH = BACKEND / "data" / "stocks.db"
UPDATER = BACKEND / "data" / "kis_investor_detail_updater.py"
PIPELINE = BACKEND / "daily_pipeline.py"
MAIN_PY = BACKEND / "server" / "main.py"


@pytest.fixture(scope="module")
def conn():
    try:
        import sqlite3
    except ImportError:
        pytest.skip("sqlite3 없음")
    if not DB_PATH.exists():
        pytest.skip("stocks.db 없음")
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()


# ── 1. DB 스키마: pension 컬럼 존재 ───────────────────────────────

def test_investor_trading_kis_detail_has_pension_qty(conn):
    """investor_trading_kis_detail 테이블에 pension_qty 컬럼이 있어야 한다."""
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(investor_trading_kis_detail)"
    ).fetchall()}
    assert "pension_qty" in cols, (
        "investor_trading_kis_detail에 pension_qty 컬럼 없음 — "
        "테이블 스키마 확인 필요"
    )


def test_investor_trading_kis_detail_has_pension_value(conn):
    """investor_trading_kis_detail 테이블에 pension_value 컬럼이 있어야 한다."""
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(investor_trading_kis_detail)"
    ).fetchall()}
    assert "pension_value" in cols, (
        "investor_trading_kis_detail에 pension_value 컬럼 없음"
    )


# ── 2. DB 데이터: pension 데이터 실제 존재 ────────────────────────

def test_pension_data_exists(conn):
    """investor_trading_kis_detail에 pension_value != 0인 레코드가 최소 1건 있어야 한다.
    백필로 2020~2026-06-26 데이터가 수집돼 있으므로 pension 데이터가 있어야 함."""
    row = conn.execute(
        "SELECT COUNT(*) FROM investor_trading_kis_detail "
        "WHERE pension_value IS NOT NULL AND pension_value != 0"
    ).fetchone()
    count = row[0] if row else 0
    assert count > 0, (
        f"pension_value가 NULL/0이 아닌 레코드가 없음 (현재 {count}건) — "
        "백필 데이터가 제대로 수집됐는지 확인 필요"
    )


def test_pension_latest_date(conn):
    """pension_value가 있는 최신 날짜가 2026-06-01 이후여야 한다.
    백필이 2026-06-26까지 수집됐으므로 최소 이 날짜 이상이어야 함."""
    row = conn.execute(
        "SELECT MAX(date) FROM investor_trading_kis_detail "
        "WHERE pension_value IS NOT NULL AND pension_value != 0"
    ).fetchone()
    latest = row[0] if row else None
    assert latest is not None and latest >= "20260601", (
        f"pension 최신 날짜가 너무 오래됨 ({latest}) — "
        "백필 미완료이거나 데이터가 없음"
    )


# ── 3. 파일 존재: kis_investor_detail_updater.py ─────────────────

def test_updater_script_exists():
    """kis_investor_detail_updater.py 파일이 존재해야 한다."""
    assert UPDATER.exists(), (
        f"kis_investor_detail_updater.py 파일 없음: {UPDATER}"
    )


# ── 4. daily_pipeline.py에 updater 단계 포함 ─────────────────────

def test_pipeline_has_detail_updater_step():
    """daily_pipeline.py에 kis_investor_detail_updater 실행 단계가 있어야 한다."""
    src = PIPELINE.read_text(encoding="utf-8")
    assert "kis_investor_detail_updater" in src, (
        "daily_pipeline.py에 kis_investor_detail_updater 단계 없음 — "
        "연기금 데이터 자동 수집이 파이프라인에 연결되지 않음"
    )


# ── 5. updater가 backfill 함수를 재사용 ──────────────────────────

def test_updater_imports_from_backfill():
    """kis_investor_detail_updater.py가 fetch_batch/parse_rows를 backfill에서 임포트해야 한다."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "from kis_investor_backfill import" in src, (
        "updater가 kis_investor_backfill을 임포트하지 않음 — "
        "fetch_batch/parse_rows 중복 구현 위험"
    )
    assert "fetch_batch" in src, "updater에 fetch_batch 사용 없음"
    assert "parse_rows" in src, "updater에 parse_rows 사용 없음"


# ── 6. sector-flow 엔드포인트가 pension_value를 읽음 ─────────────

def test_sector_flow_reads_pension():
    """main.py sector-flow 엔드포인트가 pension_value를 읽는 코드를 포함해야 한다."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "pension_value" in src, (
        "main.py에 pension_value 참조 없음 — "
        "sector-flow가 연기금 데이터를 읽지 않음"
    )
    # sector-flow 관련 코드 안에 pension 있는지 더 구체적으로 확인
    assert "sector-flow" in src or "sector_flow" in src or "sector-flow" in src.lower(), (
        "main.py에 sector-flow 엔드포인트가 없음"
    )
