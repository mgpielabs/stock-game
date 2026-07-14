"""
가격 데이터 정합성 점검 (2026-06-24, 스케일 버그 1/2단계 사후 예방).

collector.py는 증분(최신 저장일 이후)만 받아오는 구조라, 분할/병합이나 pykrx
adjusted=True의 조회구간별 조정기준 차이로 "전일 대비 비정상 비율(2.5배 이상/0.4배 이하)"이
생기면 그 이후 데이터가 계속 잘못된 스케일로 쌓일 수 있다(1단계: 백필↔증분 경계 버그,
2단계: 미조정 분할 91+건이 이 패턴이었음 — CLAUDE.md "신호 연구" 위쪽 참고).

최근 LOOKBACK_DAYS 안에서 이런 비율 이상을 보이는 종목을 찾아, 종목별 DB 전체 구간을
pykrx 단일 호출로 재조회해서 비교한다(좁은 윈도우 비교는 오판 가능 — 1단계에서 확인됨).
DB가 그 결과와 어긋나면(진짜 분할/스케일 버그) pykrx 값으로 덮어쓰고, pykrx도 동일한
점프를 보이면(진짜 시세 변동) 손대지 않는다.

daily_pipeline.py에서 호출됨. 항상 exit 0(비치명적) — 이 점검이 실패해도 일일
파이프라인 전체를 막지 않음.

실행: python price_integrity_check.py [--lookback-days 10] [--dry-run]
로그: backend/price_integrity.log
무시 목록: backend/data/.scale_check_ignore.json (예: 018620 — 패턴이 달라 별도 조사 필요한 종목)
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "stocks.db"
IGNORE_FILE = ROOT / "data" / ".scale_check_ignore.json"
DEFAULT_LOOKBACK_DAYS = 10
JUMP_RATIO_HI = 2.5
JUMP_RATIO_LO = 1 / JUMP_RATIO_HI  # 0.4
MISMATCH_RATIO_LO = 0.9
MISMATCH_RATIO_HI = 1.1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "price_integrity.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def load_ignore_list() -> set:
    if not IGNORE_FILE.exists():
        return set()
    try:
        return set(json.loads(IGNORE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def find_recent_jump_symbols(lookback_days: int) -> list:
    """최근 lookback_days 안에서 전일 대비 2.5배+/0.4배- 비율을 보인 종목 목록."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MAX(date) FROM prices")
    latest = cur.fetchone()[0]
    if not latest:
        conn.close()
        return []
    cutoff_idx_query = """
        SELECT DISTINCT symbol FROM (
            SELECT date FROM prices GROUP BY date ORDER BY date DESC LIMIT ?
        )
    """
    # 거래일 기준 최근 N일의 최소 날짜를 cutoff로 사용
    cur.execute(
        "SELECT MIN(date) FROM (SELECT DISTINCT date FROM prices ORDER BY date DESC LIMIT ?)",
        (lookback_days,),
    )
    cutoff = cur.fetchone()[0]

    cur.execute(
        """
        WITH ranked AS (
            SELECT symbol, date, close,
                   LAG(date) OVER (PARTITION BY symbol ORDER BY date) AS prev_date,
                   LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
            FROM prices
            WHERE close > 0
        )
        SELECT DISTINCT symbol
        FROM ranked
        WHERE prev_close > 0 AND date >= ?
          AND (CAST(close AS FLOAT) / prev_close >= ? OR CAST(close AS FLOAT) / prev_close <= ?)
        """,
        (cutoff, JUMP_RATIO_HI, JUMP_RATIO_LO),
    )
    symbols = [r[0] for r in cur.fetchall()]
    conn.close()
    return symbols


def get_db_range(symbol: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MIN(date), MAX(date) FROM prices WHERE symbol=?", (symbol,))
    row = cur.fetchone()
    conn.close()
    return row


def get_db_series(symbol: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT date, close FROM prices WHERE symbol=? ORDER BY date", (symbol,))
    rows = cur.fetchall()
    conn.close()
    return rows


def verify_against_pykrx(symbol: str):
    """종목 전체 구간을 pykrx 단일 호출로 재조회해서 DB와 어긋나는 날짜 목록을 반환."""
    from pykrx import stock

    start, end = get_db_range(symbol)
    if not start:
        return None, []
    try:
        df = stock.get_market_ohlcv_by_date(start, end, symbol)
    except Exception as exc:
        log.warning("pykrx 조회 실패 [%s]: %s", symbol, exc)
        return None, []
    if df is None or df.empty:
        return None, []
    df.index = df.index.strftime("%Y%m%d")

    mismatches = []
    for date, close in get_db_series(symbol):
        if date not in df.index or close <= 0:
            continue
        true_close = int(df.loc[date, "종가"])
        if true_close <= 0:
            continue
        ratio = close / true_close
        if ratio < MISMATCH_RATIO_LO or ratio > MISMATCH_RATIO_HI:
            mismatches.append(date)
    return df, mismatches


def fix_symbol(symbol: str, df, dry_run: bool) -> int:
    if dry_run:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    n = 0
    for dt, row in df.iterrows():
        date_str = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else dt
        cur.execute(
            "UPDATE prices SET open=?, high=?, low=?, close=?, volume=? WHERE symbol=? AND date=?",
            (
                int(row.get("시가", 0) or 0),
                int(row.get("고가", 0) or 0),
                int(row.get("저가", 0) or 0),
                int(row.get("종가", 0) or 0),
                int(row.get("거래량", 0) or 0),
                symbol,
                date_str,
            ),
        )
        n += cur.rowcount
    conn.commit()
    conn.close()
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description="가격 데이터 정합성 점검 (전일 대비 2.5배+ 비율 감지)")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--dry-run", action="store_true", help="감지만 하고 수정은 안 함")
    args = parser.parse_args()

    try:
        ignore = load_ignore_list()
        candidates = find_recent_jump_symbols(args.lookback_days)
        candidates = [s for s in candidates if s not in ignore]
        log.info("최근 %d일 내 비율 이상 후보: %d종목 (무시목록 %d종목 제외)", args.lookback_days, len(candidates), len(ignore))

        if not candidates:
            log.info("이상 없음")
            return 0

        n_fixed = 0
        n_real_event = 0
        for symbol in candidates:
            df, mismatches = verify_against_pykrx(symbol)
            if df is None:
                continue
            if not mismatches:
                log.info("[%s] pykrx와 일치하는 진짜 시세 변동 — 손대지 않음", symbol)
                n_real_event += 1
                continue

            log.warning("[%s] 스케일 버그 확정 — %d개 날짜 불일치, pykrx 값으로 재수집", symbol, len(mismatches))
            if args.dry_run:
                log.info("[DRY-RUN] [%s] 수정 건너뜀", symbol)
            else:
                n = fix_symbol(symbol, df, args.dry_run)
                log.info("[%s] %d행 수정 완료", symbol, n)
            n_fixed += 1

        log.info("점검 완료: 버그 수정=%d종목, 진짜 이벤트(보존)=%d종목", n_fixed, n_real_event)
        return 0
    except Exception as exc:
        log.error("정합성 점검 중 예외(비치명적): %s", exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
