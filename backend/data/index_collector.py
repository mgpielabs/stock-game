"""
KOSPI / KOSDAQ 지수 일봉 수집기
네이버 fchart API 사용 (pykrx 지수 API는 새벽 점검 시간에 빈 응답)

실행법:
  python index_collector.py --initial        # features 기간 기준 초기 전체 수집
  python index_collector.py --start 20200101 # 직접 시작일 지정
  python index_collector.py                  # 증분 (마지막 날짜 다음부터 오늘)

데이터 형식: date|open|high|low|close|volume (XML item 태그)
"""

import argparse
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Optional

# daily_pipeline.py가 자식 프로세스 stdout을 UTF-8로 디코딩하므로(run() 참고) 맞춰줌 —
# 콘솔이 cp949인 환경(수동 실행 .bat 등)에서 안 맞추면 UnicodeEncodeError 스팸 발생(2026-06-23)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
import pandas as pd

from db import init_db, get_connection, get_latest_index_date, insert_index_ohlcv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),  # 기본값(stderr)은 위 reconfigure 효과를 못 받음
        logging.FileHandler("index_collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# 네이버 fchart API — pykrx도 종목 OHLCV에 이 API 사용
FCHART_URL = "https://fchart.stock.naver.com/sise.nhn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}

# KOSPI=KOSPI, KOSDAQ=KOSDAQ (fchart symbol)
INDICES = {
    "1001": "KOSPI",
    "2001": "KOSDAQ",
}
API_DELAY = 0.5
MAX_COUNT = 3000  # 최대 약 12년치 (거래일 기준)


def _oldest_features_date() -> Optional[str]:
    """features 테이블에서 가장 오래된 날짜 반환"""
    with get_connection() as conn:
        try:
            row = conn.execute("SELECT MIN(date) FROM features").fetchone()
            return row[0] if row else None
        except Exception:
            return None


def _calc_initial_count() -> int:
    """
    features 최고 날짜 - 200거래일(280 calendar days) 이전부터 필요한 거래일 수 계산.
    fchart는 count 기반이므로 오늘부터 필요 시작일까지의 거래일 수를 반환.
    """
    oldest = _oldest_features_date()
    if oldest:
        y, m, d_ = int(oldest[:4]), int(oldest[4:6]), int(oldest[6:8])
        start_dt = date(y, m, d_) - timedelta(days=280)  # 200거래일 ≈ 280 calendar days
    else:
        start_dt = date.today() - timedelta(days=5 * 365)

    days_diff = (date.today() - start_dt).days
    # calendar days → trading days (약 70% 비율)
    count = int(days_diff * 0.72) + 50  # 여유 포함
    count = min(count, MAX_COUNT)
    logger.info("features 기준일: %s → 수집 count: %d 거래일", oldest, count)
    return count


def _fetch_index(code: str, count: int) -> pd.DataFrame:
    """
    네이버 fchart API로 지수 일봉 수집.
    count: 최근 N 거래일
    반환: DataFrame columns=[date, code, open, high, low, close, volume]
    """
    symbol = INDICES.get(code)
    if not symbol:
        logger.error("알 수 없는 지수 코드: %s", code)
        return pd.DataFrame()

    logger.info("[%s] fchart 조회 (count=%d)...", symbol, count)
    try:
        r = requests.get(
            FCHART_URL,
            params={"symbol": symbol, "timeframe": "day", "count": count, "requestType": 0},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        time.sleep(API_DELAY)
    except Exception as exc:
        logger.error("[%s] API 오류: %s", symbol, exc)
        return pd.DataFrame()

    # XML 파싱: <item data="YYYYMMDD|open|high|low|close|volume" />
    items = re.findall(r'data="([^"]+)"', r.text)
    if not items:
        logger.warning("[%s] 데이터 없음 (응답 길이: %d)", symbol, len(r.text))
        return pd.DataFrame()

    records = []
    for item in items:
        parts = item.split("|")
        if len(parts) < 5:
            continue
        try:
            records.append({
                "date":   parts[0],
                "code":   code,
                "open":   float(parts[1]),
                "high":   float(parts[2]),
                "low":    float(parts[3]),
                "close":  float(parts[4]),
                "volume": float(parts[5]) if len(parts) > 5 else None,
            })
        except (ValueError, IndexError):
            continue

    df = pd.DataFrame(records)
    logger.info("[%s] %d행 수집 완료 (%s ~ %s)",
                symbol, len(df),
                df["date"].min() if not df.empty else "-",
                df["date"].max() if not df.empty else "-")
    return df


def _filter_by_start(df: pd.DataFrame, start: Optional[str]) -> pd.DataFrame:
    """start 날짜 이후만 반환"""
    if start and not df.empty:
        df = df[df["date"] >= start]
    return df


def collect_index(count: int, start: Optional[str] = None) -> int:
    """지수 수집 후 DB 저장. start가 있으면 해당 날짜 이후만 저장."""
    init_db()
    total = 0
    for code in INDICES:
        df = _fetch_index(code, count)
        if df.empty:
            continue
        df = _filter_by_start(df, start)
        if df.empty:
            logger.info("[%s] 저장할 신규 데이터 없음", INDICES[code])
            continue
        records = df.to_dict("records")
        insert_index_ohlcv(records)
        total += len(records)
        logger.info("[%s] DB 저장: %d행", INDICES[code], len(records))

    logger.info("지수 수집 완료 | 총 %d행", total)
    return total


def collect_index_incremental() -> int:
    """마지막 저장 날짜 다음부터 오늘까지 증분 수집"""
    init_db()
    total = 0

    for code in INDICES:
        symbol = INDICES[code]
        last = get_latest_index_date(code)
        today = date.today().strftime("%Y%m%d")

        if last and last >= today:
            logger.info("[%s] 이미 최신 (%s) — 스킵", symbol, last)
            continue

        # 마지막 날짜 다음부터 오늘까지 — count는 여유있게 60 (최근 2달)
        df = _fetch_index(code, count=60)
        if df.empty:
            continue

        # 마지막 저장일 이후만 필터
        start = None
        if last:
            y, m, d_ = int(last[:4]), int(last[4:6]), int(last[6:8])
            next_day = (date(y, m, d_) + timedelta(days=1)).strftime("%Y%m%d")
            start = next_day

        df = _filter_by_start(df, start)
        if df.empty:
            logger.info("[%s] 증분 데이터 없음", symbol)
            continue

        records = df.to_dict("records")
        insert_index_ohlcv(records)
        total += len(records)
        logger.info("[%s] 증분 저장: %d행", symbol, len(records))

    return total


def main():
    parser = argparse.ArgumentParser(description="KOSPI/KOSDAQ 지수 일봉 수집")
    parser.add_argument("--initial", action="store_true",
                        help="features 기간 기준 초기 전체 수집")
    parser.add_argument("--start", type=str,
                        help="시작일 지정 (YYYYMMDD) — 이 날짜 이후 데이터만 저장")
    parser.add_argument("--count", type=int, default=None,
                        help="수집할 거래일 수 (기본: 자동 계산)")
    args = parser.parse_args()

    if args.initial or args.start:
        count = args.count or _calc_initial_count()
        start = args.start  # None이면 전체 저장
        collect_index(count=count, start=start)
    else:
        collect_index_incremental()


if __name__ == "__main__":
    main()
