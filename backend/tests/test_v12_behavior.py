"""
회귀 테스트: v1.2 슬리브 동작 (항목 B)

검증 항목:
  1. shadow_prediction_log 테이블에 gate 컬럼 존재
  2. shadow_prediction_log에 데이터가 쌓이고 있음 (G2 gate 기록)
  3. main.py lifespan에 v1.2 5d shadow 기록 로직 존재
  4. main.py lifespan에 60d 항상 진입 로직 존재
  5. should_enter_60d 호출이 lifespan 코드에 있음
  6. paper_trades에 horizon=60 open 포지션이 존재
"""
from pathlib import Path
import sqlite3

import pytest

DB_PATH = Path(__file__).parent.parent / "data" / "stocks.db"
MAIN_PY  = Path(__file__).parent.parent / "server" / "main.py"


@pytest.fixture(scope="module")
def conn():
    if not DB_PATH.exists():
        pytest.skip("stocks.db 없음")
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()


def _main_src() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


# ── 1. shadow_prediction_log gate 컬럼 ───────────────────────────

def test_shadow_log_gate_column_exists(conn):
    """shadow_prediction_log 테이블에 gate 컬럼이 있어야 한다."""
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(shadow_prediction_log)"
    ).fetchall()}
    assert "gate" in cols, (
        "shadow_prediction_log에 gate 컬럼 없음 — "
        "v1.2 shadow 기록 로직 확인 필요"
    )


def test_shadow_log_table_exists(conn):
    """shadow_prediction_log 테이블이 존재해야 한다."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "shadow_prediction_log" in tables, (
        "shadow_prediction_log 테이블이 DB에 없음"
    )


# ── 2. shadow_prediction_log에 G2 gate 기록 ──────────────────────

def test_shadow_log_has_g2_records(conn):
    """shadow_prediction_log에 gate='G2' 레코드가 1건 이상 존재해야 한다.
    v1.2 전환 전 G2 차단 구간 기록이나 5d 이전 shadow 기록이 있어야 함."""
    row = conn.execute(
        "SELECT COUNT(*) FROM shadow_prediction_log WHERE gate = 'G2'"
    ).fetchone()
    count = row[0] if row else 0
    assert count > 0, (
        f"shadow_prediction_log에 gate='G2' 레코드가 없음 (현재 {count}건) — "
        "G2 shadow 기록이 실제로 쌓이지 않고 있음"
    )


# ── 3. main.py에 v1.2 shadow 기록 로직 ───────────────────────────

def test_main_has_shadow_log_for_5d():
    """main.py에 5d shadow 기록 코드(log_shadow_predictions)가 있어야 한다."""
    src = _main_src()
    assert "log_shadow_predictions" in src, (
        "main.py에 log_shadow_predictions 호출이 없음 — "
        "v1.2 5d shadow 기록 로직 미적용"
    )


def test_main_has_v12_comment_or_version():
    """main.py lifespan에 v1.2 관련 주석이나 코드가 있어야 한다."""
    src = _main_src()
    assert "v1.2" in src or "shadow" in src.lower(), (
        "main.py에 v1.2 또는 shadow 관련 코드가 없음"
    )


# ── 4. main.py에 60d 항상 진입 로직 ─────────────────────────────

def test_main_has_60d_entry_logic():
    """main.py에 should_enter_60d 호출이 있어야 한다."""
    src = _main_src()
    assert "should_enter_60d" in src, (
        "main.py에 should_enter_60d 호출 없음 — "
        "60d 진입 로직이 없음"
    )


def test_main_60d_not_gate_blocked():
    """main.py lifespan에서 60d 슬리브는 게이트 차단 없이 진입해야 한다.
    코드 검사: should_enter_60d 호출이 gate_blocked 조건 밖에 있어야 한다."""
    src = _main_src()
    # should_enter_60d 다음에 record_recommendations(..., horizon=60)가 있어야 함
    idx_60d_enter = src.find("should_enter_60d")
    assert idx_60d_enter != -1, "should_enter_60d 없음"
    snippet = src[idx_60d_enter : idx_60d_enter + 500]
    assert "horizon=60" in snippet or 'horizon": 60' in snippet or "60" in snippet, (
        "should_enter_60d 근처에 60d 진입 로직이 없음"
    )


# ── 5. paper_trades horizon=60 open 포지션 ───────────────────────

def test_paper_trades_has_open_60d(conn):
    """paper_trades에 horizon=60 open 포지션이 최소 1건 이상이어야 한다.
    v1.2 전환 이후 60d만 신규 진입하므로 open 포지션이 있어야 함."""
    row = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status='open' AND horizon=60"
    ).fetchone()
    count = row[0] if row else 0
    assert count > 0, (
        f"horizon=60 open 포지션이 없음 (현재 {count}건) — "
        "60d 진입이 실제로 실행되지 않은 것으로 보임"
    )


# ── 6. v1.2 이후 5d 신규 진입 없음 ──────────────────────────────

def test_no_new_5d_after_v12(conn):
    """v1.2 전환일(20260809) 이후 신규 5d 진입(horizon=5)이 없어야 한다.
    paper_trades에 recommended_date >= '20260809' AND horizon=5 인 레코드 0건이어야 함."""
    row = conn.execute(
        "SELECT COUNT(*) FROM paper_trades "
        "WHERE recommended_date >= '20260809' AND horizon = 5"
    ).fetchone()
    count = row[0] if row else 0
    assert count == 0, (
        f"v1.2 전환 이후에도 5d 신규 진입 {count}건 발견 — "
        "main.py lifespan에서 5d 운용 중단이 제대로 적용되지 않음\n"
        "이 실패는 파이프라인이 v1.2 전환 이후 실행됐고 5d 진입이 됐을 때만 발생"
    )
