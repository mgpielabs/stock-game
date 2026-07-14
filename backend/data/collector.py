"""
KOSPI + KOSDAQ 전종목 데이터 수집기

데이터 소스 (실제 작동 확인 기준):
  - 종목 목록:   Naver Finance sise_market_sum 페이지 파싱
  - 종목 이름:   Naver m.stock.naver.com/api/stock/{code}/basic
  - OHLCV:       pykrx get_market_ohlcv_by_date (Naver fchart 내부 사용)
  - 시총/PER/PBR/외국인비율:  Naver integration API

증분 업데이트: INSERT OR IGNORE → 이미 있는 날짜 자동 스킵
"""

import re
import sys
import time
import socket
import logging
import argparse
from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict

# daily_pipeline.py가 자식 프로세스 stdout을 UTF-8로 디코딩하므로(run() 참고) 맞춰줌 —
# 콘솔이 cp949인 환경(수동 실행 .bat 등)에서 안 맞추면 UnicodeEncodeError 스팸 발생(2026-06-23)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# pykrx 내부 HTTP 요청에 소켓 타임아웃 적용 (무한 대기 방지)
socket.setdefaulttimeout(25)

import requests
import pandas as pd
from pykrx import stock as krx
from tqdm import tqdm

from db import (
    init_db,
    upsert_stocks,
    insert_prices,
    insert_fundamentals,
    insert_flows,
    get_latest_date,
    get_all_symbols,
)

# ── 로깅 ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),  # 기본값(stderr)은 위 reconfigure 효과를 못 받음
        logging.FileHandler("collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── 상수 ─────────────────────────────────────────────────────
DEFAULT_YEARS  = 5
API_DELAY      = 0.3     # 요청 간격(초)
MAX_RETRY      = 3
THREAD_WORKERS = 16      # 병렬 종목 수집 쓰레드 (I/O bound → 높여도 무방)

NAVER_M_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://m.stock.naver.com/",
}
NAVER_PC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}


# ── 유틸 ─────────────────────────────────────────────────────
def today_str() -> str:
    return date.today().strftime("%Y%m%d")


def default_start() -> str:
    return (date.today() - timedelta(days=DEFAULT_YEARS * 365)).strftime("%Y%m%d")


def _retry(fn, *args, max_retry=MAX_RETRY, delay=API_DELAY, **kwargs):
    for attempt in range(1, max_retry + 1):
        try:
            result = fn(*args, **kwargs)
            time.sleep(delay)
            return result
        except Exception as exc:
            logger.debug("재시도 %d/%d [%s]: %s", attempt, max_retry, fn.__name__, exc)
            if attempt == max_retry:
                raise
            time.sleep(delay * attempt)


def _parse_krw(value_str: Optional[str]) -> Optional[int]:
    """'1,555조 1,101억' → 억원 정수"""
    if not value_str:
        return None
    s = str(value_str).replace(",", "")
    result = 0.0
    m_jo = re.search(r"([\d.]+)조", s)
    m_uk = re.search(r"([\d.]+)억", s)
    if m_jo:
        result += float(m_jo.group(1)) * 10000
    if m_uk:
        result += float(m_uk.group(1))
    return int(result) if result > 0 else None


def _parse_pct(value_str: Optional[str]) -> Optional[float]:
    """'49.37%' → 49.37"""
    if not value_str:
        return None
    try:
        return float(str(value_str).replace("%", "").replace(",", "").strip())
    except ValueError:
        return None


def _parse_ratio(value_str: Optional[str]) -> Optional[float]:
    """'40.52배' → 40.52"""
    if not value_str:
        return None
    try:
        return float(str(value_str).replace("배", "").replace(",", "").strip())
    except ValueError:
        return None


# ── Step 1: 종목 목록 수집 ────────────────────────────────────
def fetch_naver_tickers(market_code: int) -> list[str]:
    """
    Naver Finance 시장 목록 페이지에서 6자리 종목코드 추출
    market_code: 0=KOSPI, 1=KOSDAQ
    """
    market_name = "KOSPI" if market_code == 0 else "KOSDAQ"
    tickers: set[str] = set()
    page = 1
    empty_streak = 0

    while True:
        try:
            r = requests.get(
                "https://finance.naver.com/sise/sise_market_sum.naver",
                params={"sosok": market_code, "page": page},
                headers=NAVER_PC_HEADERS,
                timeout=15,
            )
            codes = re.findall(r"code=(\d{6})", r.text)
            if not codes:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
                tickers.update(codes)
            page += 1
            time.sleep(0.15)
        except Exception as exc:
            logger.warning("%s 페이지 %d 오류: %s", market_name, page, exc)
            break

    logger.info("%s 종목 수집 완료: %d개", market_name, len(tickers))
    return list(tickers)


def fetch_stock_name(code: str) -> str:
    """Naver basic API로 종목명 조회"""
    try:
        r = requests.get(
            f"https://m.stock.naver.com/api/stock/{code}/basic",
            headers=NAVER_M_HEADERS,
            timeout=10,
        )
        d = r.json()
        return d.get("stockName") or d.get("reutersCode") or code
    except Exception:
        return code


def collect_tickers() -> List[Dict]:
    """전종목 마스터 수집 → stocks 테이블 upsert"""
    records: List[Dict] = []

    for market_code, market_name in [(0, "KOSPI"), (1, "KOSDAQ")]:
        tickers = fetch_naver_tickers(market_code)

        # 종목명을 병렬로 가져오기
        with ThreadPoolExecutor(max_workers=16) as pool:
            future_map = {pool.submit(fetch_stock_name, t): t for t in tickers}
            for future in tqdm(
                as_completed(future_map),
                total=len(tickers),
                desc=f"{market_name} 종목명",
                unit="종목",
            ):
                ticker = future_map[future]
                try:
                    name = future.result()
                except Exception:
                    name = ticker
                records.append({
                    "symbol": ticker,
                    "name": name,
                    "market": market_name,
                    "sector": None,
                })

    upsert_stocks(records)
    logger.info("종목 마스터 저장 완료: %d개", len(records))
    return records


# ── Step 2: OHLCV 수집 ───────────────────────────────────────

# ── pykrx 타임아웃 래퍼 (multiprocessing — 스레드와 달리 진짜 kill 가능) ──
import multiprocessing as _mp

def _pykrx_worker(q: _mp.Queue, start: str, end: str, symbol: str) -> None:
    """별도 프로세스에서 pykrx 호출 — 부모가 kill()로 강제 종료 가능"""
    try:
        from pykrx import stock as _krx
        df = _krx.get_market_ohlcv_by_date(start, end, symbol)
        q.put(df)
    except Exception:
        q.put(None)


def _pykrx_with_timeout(start: str, end: str, symbol: str, timeout: int = 20):
    """pykrx 호출을 별도 프로세스에서 실행, timeout 초 내 응답 없으면 강제 종료"""
    ctx = _mp.get_context("spawn")
    q   = ctx.Queue()
    p   = ctx.Process(target=_pykrx_worker, args=(q, start, end, symbol), daemon=True)
    p.start()
    p.join(timeout=timeout)
    if p.is_alive():
        logger.debug("OHLCV 타임아웃 [%s] — 프로세스 강제 종료", symbol)
        p.kill()
        p.join()
        return None
    return q.get() if not q.empty() else None


def _collect_ohlcv_for_symbol(symbol: str, start: str, end: str) -> List[Dict]:
    """pykrx get_market_ohlcv_by_date 로 단일 종목 OHLCV 수집 (증분: 최신 저장일 이후만 fetch)"""
    latest = get_latest_date(symbol)
    if latest:
        end_dt    = datetime.strptime(end,    "%Y%m%d")
        latest_dt = datetime.strptime(latest, "%Y%m%d")
        if latest_dt >= end_dt:
            return []
        # 건너뛰지 않고 항상 마지막 저장일 다음날부터 fetch
        start = (latest_dt + timedelta(days=1)).strftime("%Y%m%d")

    try:
        df = _pykrx_with_timeout(start, end, symbol, timeout=20)
        if df is None or df.empty:
            return []

        records = []
        for dt, row in df.iterrows():
            date_str = dt.strftime("%Y%m%d")
            records.append({
                "symbol": symbol,
                "date": date_str,
                "open": int(row.get("시가", 0) or 0),
                "high": int(row.get("고가", 0) or 0),
                "low": int(row.get("저가", 0) or 0),
                "close": int(row.get("종가", 0) or 0),
                "volume": int(row.get("거래량", 0) or 0),
                "market_cap": None,
            })
        return records
    except Exception as exc:
        logger.debug("OHLCV 오류 [%s]: %s", symbol, exc)
        return []


def collect_prices(symbols: List[Dict], start: str, end: str):
    """전종목 OHLCV 병렬 수집 (증분: 이미 최신인 종목은 API 호출 생략)"""
    logger.info("▶ OHLCV 수집: %s ~ %s (%d개 종목)", start, end, len(symbols))

    skipped = fetched = 0
    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
        futures = {
            pool.submit(_collect_ohlcv_for_symbol, s["symbol"], start, end): s["symbol"]
            for s in symbols
        }
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="OHLCV",
            unit="종목",
        ):
            sym = futures[future]
            try:
                records = future.result()
                if records:
                    insert_prices(records)
                    fetched += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.error("OHLCV 저장 오류 [%s]: %s", sym, exc)

    logger.info("OHLCV 완료: 신규/업데이트=%d, 이미최신(스킵)=%d", fetched, skipped)


# ── Step 3: 시총 + PER/PBR + 외국인비율 ──────────────────────
def _fetch_naver_integration(code: str) -> Optional[Dict]:
    """
    Naver integration API → totalInfos 파싱
    반환: { market_cap(억원), per, pbr, div_yield, foreign_rate(%) }
    """
    try:
        r = requests.get(
            f"https://m.stock.naver.com/api/stock/{code}/integration",
            headers=NAVER_M_HEADERS,
            timeout=10,
        )
        if not r.ok:
            return None
        d = r.json()
        ti: List[Dict] = d.get("totalInfos", [])
        info = {item["code"]: item.get("value") for item in ti}
        return {
            "market_cap":   _parse_krw(info.get("marketValue")),
            "per":          _parse_ratio(info.get("per")),
            "pbr":          _parse_ratio(info.get("pbr")),
            "div_yield":    _parse_pct(info.get("dividendYieldRatio")),
            "foreign_rate": _parse_pct(info.get("foreignRate")),
        }
    except Exception as exc:
        logger.debug("integration 오류 [%s]: %s", code, exc)
        return None


def collect_fundamentals_and_flows(symbols: List[Dict]):
    """
    오늘 날짜 기준으로 전종목의 시총/PER/PBR/외국인비율 수집
    - fundamentals 테이블: per, pbr, div_yield
    - prices 테이블의 market_cap 컬럼도 오늘치 UPDATE
    - flows 테이블: foreign_rate (외국인 보유비율, 일별 변화로 순매수 추정)
    """
    today = today_str()
    logger.info("▶ 시총/PER/PBR/외국인비율 수집 (오늘: %s, %d개 종목)", today, len(symbols))

    fund_records: List[Dict] = []
    flow_records: List[Dict] = []

    def _fetch(s: dict):
        info = _fetch_naver_integration(s["symbol"])
        if not info:
            return
        fund_records.append({
            "symbol":    s["symbol"],
            "date":      today,
            "per":       info["per"],
            "pbr":       info["pbr"],
            "div_yield": info["div_yield"],
        })
        flow_records.append({
            "symbol":      s["symbol"],
            "date":        today,
            "foreign_net": info["foreign_rate"],   # 외국인 보유비율(%)로 저장
            "inst_net":    None,
        })

    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
        list(tqdm(
            pool.map(_fetch, symbols),
            total=len(symbols),
            desc="시총/PER/PBR",
            unit="종목",
        ))

    if fund_records:
        insert_fundamentals(fund_records)
    if flow_records:
        insert_flows(flow_records)

    logger.info("펀더멘털 저장: %d건, 수급 저장: %d건", len(fund_records), len(flow_records))


# ── 증분 업데이트 진입점 ──────────────────────────────────────
def collect_incremental(start: Optional[str] = None, end: Optional[str] = None):
    """
    증분 업데이트:
    - start 미지정 → 3년 전 기본값
    - end 미지정   → 오늘
    - INSERT OR IGNORE → 이미 저장된 날짜는 자동 스킵
    """
    end   = end   or today_str()
    start = start or default_start()

    logger.info("=" * 60)
    logger.info("수집 시작 | %s ~ %s", start, end)
    logger.info("=" * 60)

    init_db()

    # 1. 종목 마스터
    logger.info("[1/3] 종목 마스터 갱신")
    symbols = collect_tickers()

    # 2. OHLCV (증분: INSERT OR IGNORE)
    logger.info("[2/3] OHLCV 수집")
    collect_prices(symbols, start, end)

    # 3. 펀더멘털 + 수급 (오늘치만)
    logger.info("[3/3] 시총/PER/PBR/외국인비율 (오늘 기준)")
    collect_fundamentals_and_flows(symbols)

    logger.info("=" * 60)
    logger.info("수집 완료")
    logger.info("=" * 60)


# ── CLI ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="주식 데이터 수집기")
    parser.add_argument("--start",     type=str,  help="시작일 YYYYMMDD (기본: 3년 전)")
    parser.add_argument("--end",       type=str,  help="종료일 YYYYMMDD (기본: 오늘)")
    parser.add_argument("--init-only", action="store_true", help="DB 초기화만 하고 종료")
    parser.add_argument("--tickers-only", action="store_true", help="종목 마스터만 갱신")
    parser.add_argument("--ohlcv-only",   action="store_true", help="OHLCV만 수집")
    parser.add_argument(
        "--workers", type=int, default=None,
        help="병렬 수집 스레드 수 (기본 16). 전체 재학습처럼 장기간 전종목 수집 시 "
             "낮춰서(예: 6) 대역폭 점유를 줄일 수 있음 — 일일 증분 수집은 기본값 유지 권장",
    )
    args = parser.parse_args()

    if args.workers is not None:
        global THREAD_WORKERS
        THREAD_WORKERS = max(1, args.workers)
        logger.info("병렬 스레드 수 재설정: %d", THREAD_WORKERS)

    if args.init_only:
        init_db()
        import db as _db
        print("DB 초기화 완료:", _db.DB_PATH)
        return

    if args.tickers_only:
        init_db()
        collect_tickers()
        return

    if args.ohlcv_only:
        init_db()
        symbols = get_all_symbols()
        if not symbols:
            print("종목 마스터가 비어 있습니다. 먼저 --tickers-only를 실행하세요.")
            return
        collect_prices(symbols, args.start or default_start(), args.end or today_str())
        return

    collect_incremental(start=args.start, end=args.end)


if __name__ == "__main__":
    main()
