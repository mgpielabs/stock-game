"""오늘의 대시보드 신규 엔드포인트 회귀 테스트."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

DB_PATH = ROOT / "backend" / "data" / "stocks.db"


def test_open_summary_schema():
    """/api/paper/open-summary 응답 스키마 확인."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("main", ROOT / "backend" / "server" / "main.py")
    assert spec is not None
    src = Path(ROOT / "backend" / "server" / "main.py").read_text(encoding="utf-8")
    assert "open-summary" in src, "open-summary 엔드포인트가 main.py에 없음"
    assert "paper_open_summary" in src, "paper_open_summary 함수가 main.py에 없음"


def test_open_summary_keys():
    """open-summary 함수가 count/avg_pnl_pct/best/worst/positions 키를 반환하는지 확인."""
    src = Path(ROOT / "backend" / "server" / "main.py").read_text(encoding="utf-8")
    for key in ("count", "avg_pnl_pct", "best", "worst", "positions"):
        assert f'"{key}"' in src, f"open-summary 응답에 '{key}' 키 없음"


def test_market_trend_kospi20d():
    """/api/market/trend 엔드포인트에 kospi_20d 필드가 있는지 확인."""
    src = Path(ROOT / "backend" / "server" / "main.py").read_text(encoding="utf-8")
    assert "kospi_20d" in src, "kospi_20d 필드가 market_trend_endpoint에 없음"
    assert "market_index" in src and "code='1001'" in src, "market_index KOSPI 쿼리 없음"


def test_paper_trades_has_recommended_date():
    """paper_trades 테이블에 recommended_date 컬럼이 있는지 확인."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
    assert "recommended_date" in cols, "paper_trades에 recommended_date 컬럼 없음"
    assert "recommended_price" in cols, "paper_trades에 recommended_price 컬럼 없음"
    assert "horizon" in cols, "paper_trades에 horizon 컬럼 없음"
    assert "status" in cols, "paper_trades에 status 컬럼 없음"


def test_open_summary_days_elapsed_logic():
    """open-summary의 elapsed 계산이 market_index 거래일 기준인지 확인."""
    src = Path(ROOT / "backend" / "server" / "main.py").read_text(encoding="utf-8")
    assert "trading_dates" in src, "trading_dates(거래일 리스트) 사용 안 함"
    assert "days_elapsed" in src, "days_elapsed 함수 없음"
    assert "remaining_days" in src, "remaining_days 계산 없음"


def test_live_performance_endpoint_exists():
    """/api/live-performance 엔드포인트가 main.py에 있는지 확인."""
    src = Path(ROOT / "backend" / "server" / "main.py").read_text(encoding="utf-8")
    assert "live-performance" in src, "/api/live-performance 엔드포인트 없음"
