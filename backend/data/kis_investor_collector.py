"""
한국투자증권(KIS) Open API 기반 종목별 투자자매매동향(개인/외국인/기관계) 일별 수집기.

⚠️ 연기금은 분리되지 않음 — KIS의 종목별 API(inquire-investor, FHKST01010900)는
개인/외국인/기관계(증권사+보험+투신+사모+은행+연기금 등 전부 합산) 3종만 제공.
연기금이 분리되는 API(inquire-investor-daily-by-market, FHPTJ04040000)는 시장/지수
전체 단위라 종목별로는 적용 불가 — 실제 호출로 확인됨(2026-06-27, backend/scripts/
kis_investor_field_test.py).

⚠️ 과거 백필 한계: 이 API는 날짜 구간을 직접 지정할 수 없고, 호출 시점 기준 최근
~30거래일을 롤링으로 돌려줌. "최대한 과거까지 백필"은 이 30거래일 한도가 사실상
전부 — 그보다 이전 데이터는 KIS에서 제공하지 않음. 매일 누적해서 쌓아가는 용도.

토큰 관리: KIS는 토큰 발급에 분당 호출 제한이 있어 파일 캐시(.kis_token_cache.json)로
재사용. 호출 레이트는 sector_collector.py/bps_collector.py와 동일하게 동시성 3, 호출당
지연을 둬서 과속 차단을 피함.

실행:
  python kis_investor_collector.py [--max-jobs N] [--symbols 005930,000660] [--dry-run]
로그: backend/kis_investor_collect.log
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    BACKEND_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BACKEND_DIR / ".env")
except ImportError:
    BACKEND_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).parent))
from db import DB_PATH, get_connection, insert_investor_trading_kis  # noqa: E402

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE_PATH = Path(__file__).parent / ".kis_token_cache.json"

THREAD_WORKERS = 3
REQUEST_DELAY = 0.25

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BACKEND_DIR / "kis_investor_collect.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


from kis_auth import get_access_token  # noqa: E402 — 공용 토큰 모듈 (kis_auth.py)


def fetch_investor_trend(token: str, symbol: str) -> list:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010900",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
        headers=headers, params=params, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"{symbol}: {data.get('msg1')}")
    return data.get("output", [])


def _to_int(v) -> int:
    if v is None or v == "":
        return 0
    return int(v)


def parse_records(symbol: str, output: list) -> list:
    records = []
    for row in output:
        records.append({
            "symbol": symbol,
            "date": row.get("stck_bsop_date"),
            "close": _to_int(row.get("stck_clpr")),
            "indiv_net_qty": _to_int(row.get("prsn_ntby_qty")),
            "foreign_net_qty": _to_int(row.get("frgn_ntby_qty")),
            "inst_net_qty": _to_int(row.get("orgn_ntby_qty")),
            "indiv_net_value": _to_int(row.get("prsn_ntby_tr_pbmn")),
            "foreign_net_value": _to_int(row.get("frgn_ntby_tr_pbmn")),
            "inst_net_value": _to_int(row.get("orgn_ntby_tr_pbmn")),
        })
    return records


def load_target_symbols(limit_symbols=None) -> list:
    if limit_symbols:
        return limit_symbols
    with get_connection() as conn:
        rows = conn.execute("SELECT symbol, name FROM stocks ORDER BY symbol").fetchall()
    return [sym for sym, name in rows if not ETF_PATTERN.search(name or "")]


def _try_one(token: str, sym: str):
    time.sleep(REQUEST_DELAY)
    output = fetch_investor_trend(token, sym)
    return sym, output


def _process_batch(token: str, symbols: list, workers: int) -> tuple:
    """주어진 종목 리스트를 수집 시도. (성공수, 데이터없음수, 실패종목리스트, 적립행수) 반환."""
    n_ok = n_no_data = total_rows = 0
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_try_one, token, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                sym, output = future.result()
                if not output:
                    n_no_data += 1
                    continue
                records = parse_records(sym, output)
                insert_investor_trading_kis(records)
                total_rows += len(records)
                n_ok += 1
            except Exception as exc:
                log.debug("수집 실패 [%s]: %s", sym, exc)
                failed.append(sym)
    return n_ok, n_no_data, failed, total_rows


def run(max_jobs: int, limit_symbols, dry_run: bool):
    token = get_access_token()
    symbols = load_target_symbols(limit_symbols)
    if max_jobs:
        symbols = symbols[:max_jobs]
    log.info("수집 대상: %d종목", len(symbols))

    if dry_run:
        log.info("[DRY-RUN] 대상만 출력하고 종료: %s", symbols[:10])
        return

    n_ok, n_no_data, failed, total_rows = _process_batch(token, symbols, THREAD_WORKERS)

    # 1차 실패는 대부분 일시적 rate-limit성(재현 테스트로 확인) — 동시성을 줄여서 최대 2회 재시도
    retry_round = 0
    while failed and retry_round < 2:
        retry_round += 1
        log.info("1차 실패 %d건 — %d차 재시도(동시성 1, 지연 1초)", len(failed), retry_round)
        time.sleep(3)
        global REQUEST_DELAY
        prev_delay = REQUEST_DELAY
        REQUEST_DELAY = 1.0
        try:
            r_ok, r_no_data, failed, r_rows = _process_batch(token, failed, 1)
        finally:
            REQUEST_DELAY = prev_delay
        n_ok += r_ok
        n_no_data += r_no_data
        total_rows += r_rows

    log.info(
        "수집 완료: 성공=%d, 데이터없음=%d, 최종실패=%d, 총 적립 행=%d",
        n_ok, n_no_data, len(failed), total_rows,
    )
    if failed:
        log.warning("최종 실패 종목: %s", failed)


def main():
    parser = argparse.ArgumentParser(description="KIS 종목별 투자자매매동향 수집")
    parser.add_argument("--max-jobs", type=int, default=0, help="0=전체 종목")
    parser.add_argument("--symbols", type=str, default=None, help="쉼표구분 종목코드(테스트용)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not APP_KEY or not APP_SECRET:
        log.error("KIS_APP_KEY / KIS_APP_SECRET이 .env에 없습니다.")
        sys.exit(1)

    limit_symbols = args.symbols.split(",") if args.symbols else None
    run(args.max_jobs, limit_symbols, args.dry_run)


if __name__ == "__main__":
    main()
