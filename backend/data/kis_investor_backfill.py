"""
KIS investor-trade-by-stock-daily(FHPTJ04160001) 기반 종목별 투자자매매동향 다년치 백필.

investor_trading_kis(inquire-investor, 30거래일 롤링 한정)과 별개 — 이 API는 단일 날짜
앵커(FID_INPUT_DATE_1) 기준 직전 30거래일을 주고, 앵커를 과거로 옮겨가며 반복 호출하면
임의 시점까지 백필 가능함(2026-06-27 실제 호출로 2021년대까지 확인됨). 기관을 8개로
세분화(증권/투신/사모/은행/보험/종금/기금=연기금/기타)하고 외국인도 등록/비등록 분리.

중단/재개: investor_backfill_progress 체크포인트 테이블에 종목별 oldest_date_collected를
기록 — 호출 성공마다 즉시 커밋하므로 중간에 프로세스가 죽어도 그 지점부터 재개 가능.
다음 앵커 = oldest_date_collected - 1일(달력) — API가 그 이전 가장 가까운 30거래일을
줘서 자연스럽게 이어붙음.

레이트리밋: 3스레드(종목 단위 분산) + 0.3초 지연. 실패 종목은 1스레드/1초로 자동 재시도
(kis_investor_collector.py와 동일 패턴).

실행:
  python kis_investor_backfill.py --target-start 20210101 --symbols 005930,000660 [--dry-run]
  python kis_investor_backfill.py --target-start 20210101 --max-symbols 10
로그: backend/kis_investor_backfill.log
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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
from db import (  # noqa: E402
    DB_PATH, get_connection, insert_investor_trading_kis_detail,
    get_backfill_progress, upsert_backfill_progress,
)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE_PATH = Path(__file__).parent / ".kis_token_cache.json"

THREAD_WORKERS = 1   # 실측 결과 3스레드 동시시작은 거의 항상 즉시 차단됨 — 1스레드가 유일하게 안정적
REQUEST_DELAY = 1.0
RETRY_WORKERS = 1
RETRY_DELAY = 1.5
MAX_RETRY_ROUNDS = 2

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BACKEND_DIR / "kis_investor_backfill.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


from kis_auth import get_access_token  # noqa: E402 — 공용 토큰 모듈 (kis_auth.py)


_LATEST_TRADING_DAY = None


def _latest_trading_day() -> str:
    """prices 테이블의 가장 최근 거래일. 신규 시작 종목의 앵커로 사용 —
    datetime.now()(오늘 날짜)는 00:00~15:40 KST 사이 이 API의 'TIME LIMIT' 거부와
    충돌하므로(2026-06-27 백필에서 자정 이후 신규 시작 종목 전부 실패 확인), 항상
    과거 확정 거래일을 앵커로 써서 시간대 제한과 무관하게 만든다."""
    global _LATEST_TRADING_DAY
    if _LATEST_TRADING_DAY is None:
        with get_connection() as conn:
            row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
        _LATEST_TRADING_DAY = (
            row[0] if row and row[0] else (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        )
    return _LATEST_TRADING_DAY


def fetch_batch(token: str, symbol: str, anchor_date: str) -> list:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPTJ04160001",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": anchor_date,
        "FID_ORG_ADJ_PRC": "",
        "FID_ETC_CLS_CODE": "",
    }
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
        headers=headers, params=params, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"{symbol}@{anchor_date}: {data.get('msg1')}")
    return data.get("output2", [])


def _to_int(v) -> int:
    if v is None or v == "":
        return 0
    return int(v)


def parse_rows(symbol: str, output2: list) -> list:
    records = []
    for row in output2:
        records.append({
            "symbol": symbol,
            "date": row.get("stck_bsop_date"),
            "close": _to_int(row.get("stck_clpr")),
            # 순매수 수량
            "indiv_qty": _to_int(row.get("prsn_ntby_qty")),
            "foreign_qty": _to_int(row.get("frgn_ntby_qty")),
            "foreign_reg_qty": _to_int(row.get("frgn_reg_ntby_qty")),
            "foreign_nreg_qty": _to_int(row.get("frgn_nreg_ntby_qty")),
            "inst_total_qty": _to_int(row.get("orgn_ntby_qty")),
            "securities_qty": _to_int(row.get("scrt_ntby_qty")),
            "trust_qty": _to_int(row.get("ivtr_ntby_qty")),
            "pe_fund_qty": _to_int(row.get("pe_fund_ntby_vol")),
            "bank_qty": _to_int(row.get("bank_ntby_qty")),
            "insurance_qty": _to_int(row.get("insu_ntby_qty")),
            "merchant_bank_qty": _to_int(row.get("mrbn_ntby_qty")),
            "pension_qty": _to_int(row.get("fund_ntby_qty")),
            "etc_qty": _to_int(row.get("etc_ntby_qty")),
            # 순매수 거래대금 — 외국인 등록/비등록만 필드명에 "tr_" 누락(KIS 응답 자체 불일치)
            "indiv_value": _to_int(row.get("prsn_ntby_tr_pbmn")),
            "foreign_value": _to_int(row.get("frgn_ntby_tr_pbmn")),
            "foreign_reg_value": _to_int(row.get("frgn_reg_ntby_pbmn")),
            "foreign_nreg_value": _to_int(row.get("frgn_nreg_ntby_pbmn")),
            "inst_total_value": _to_int(row.get("orgn_ntby_tr_pbmn")),
            "securities_value": _to_int(row.get("scrt_ntby_tr_pbmn")),
            "trust_value": _to_int(row.get("ivtr_ntby_tr_pbmn")),
            "pe_fund_value": _to_int(row.get("pe_fund_ntby_tr_pbmn")),
            "bank_value": _to_int(row.get("bank_ntby_tr_pbmn")),
            "insurance_value": _to_int(row.get("insu_ntby_tr_pbmn")),
            "merchant_bank_value": _to_int(row.get("mrbn_ntby_tr_pbmn")),
            "pension_value": _to_int(row.get("fund_ntby_tr_pbmn")),
            "etc_value": _to_int(row.get("etc_ntby_tr_pbmn")),
        })
    return records


def backfill_one_symbol(token: str, symbol: str, target_start: str, delay: float) -> dict:
    """한 종목을 target_start까지 백필. 이미 진행된 게 있으면 거기서부터 재개."""
    progress = get_backfill_progress(symbol)
    if progress and progress["status"] == "done":
        return {"symbol": symbol, "status": "already_done", "n_calls": 0}
    if progress and progress["status"] == "no_more_data":
        return {"symbol": symbol, "status": "already_no_more_data", "n_calls": 0}

    anchor = None
    if progress and progress["oldest_date_collected"]:
        oldest_dt = datetime.strptime(progress["oldest_date_collected"], "%Y%m%d")
        anchor = (oldest_dt - timedelta(days=1)).strftime("%Y%m%d")
    else:
        anchor = _latest_trading_day()

    n_calls = 0
    oldest_seen = progress["oldest_date_collected"] if progress else None
    while True:
        time.sleep(delay)
        try:
            output2 = fetch_batch(token, symbol, anchor)
        except Exception as exc:
            err = str(exc)
            # KIS 측 시간대 제한(00:00~15:40 KST엔 이 TR 자체가 응답 안 함) — 한도/인증 문제가
            # 아니라 같은 실행 안에서 즉시 재시도해도 무의미하므로 별도 상태로 구분해 재시도
            # 큐에서 빼고, 체크포인트만 남겨 다음 실행 때(또는 15:40 이후) 자연스럽게 재개되게 함.
            status = "time_limit" if "TIME LIMIT" in err else "failed"
            upsert_backfill_progress(symbol, target_start, oldest_seen, status, n_calls_delta=1, last_error=err)
            return {"symbol": symbol, "status": status, "n_calls": n_calls, "error": err}

        n_calls += 1
        if not output2:
            upsert_backfill_progress(symbol, target_start, oldest_seen, "no_more_data", n_calls_delta=1)
            return {"symbol": symbol, "status": "no_more_data", "n_calls": n_calls, "oldest": oldest_seen}

        records = parse_rows(symbol, output2)
        insert_investor_trading_kis_detail(records)
        dates = [r["date"] for r in records if r["date"]]
        if not dates:
            upsert_backfill_progress(symbol, target_start, oldest_seen, "no_more_data", n_calls_delta=1)
            return {"symbol": symbol, "status": "no_more_data", "n_calls": n_calls, "oldest": oldest_seen}
        batch_oldest = min(dates)
        oldest_seen = batch_oldest if not oldest_seen else min(oldest_seen, batch_oldest)

        if oldest_seen <= target_start:
            upsert_backfill_progress(symbol, target_start, oldest_seen, "done", n_calls_delta=1)
            return {"symbol": symbol, "status": "done", "n_calls": n_calls, "oldest": oldest_seen}

        upsert_backfill_progress(symbol, target_start, oldest_seen, "in_progress", n_calls_delta=1)
        anchor_dt = datetime.strptime(batch_oldest, "%Y%m%d") - timedelta(days=1)
        anchor = anchor_dt.strftime("%Y%m%d")


def load_target_symbols(limit_symbols=None, order_by_value=True) -> list:
    """거래대금(20일 평균, close*volume) 큰 종목부터 우선 처리 — market_cap이 DB에
    없어서(NULL) 대신 사용하는 사이즈 프록시, dataset.py의 사이즈 프록시와 동일 방식."""
    if limit_symbols:
        return limit_symbols
    with get_connection() as conn:
        names = {sym: name for sym, name in conn.execute("SELECT symbol, name FROM stocks").fetchall()}
        if not order_by_value:
            symbols = sorted(names.keys())
        else:
            rows = conn.execute(
                """
                SELECT symbol, AVG(close * volume) AS value_ma
                FROM (
                    SELECT symbol, close, volume, date,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                    FROM prices
                )
                WHERE rn <= 20
                GROUP BY symbol
                ORDER BY value_ma DESC
                """
            ).fetchall()
            ranked = [sym for sym, _ in rows]
            ranked_set = set(ranked)
            symbols = ranked + sorted(s for s in names if s not in ranked_set)
    return [s for s in symbols if not ETF_PATTERN.search(names.get(s, "") or "")]


def show_status():
    # 백필 본체(load_target_symbols)와 동일한 ETF_PATTERN 기준 유니버스로 분모를 맞춤 —
    # 기존 LIKE '%ETF%' 단순 필터는 ETF_PATTERN보다 허술해서 실제 대상(2,879)보다
    # 부정확하게 큰 값(3,577)을 보여주던 버그(2026-06-27 --status 첫 실행 때 발견, 미수정 상태였음).
    total = len(load_target_symbols(order_by_value=False))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*), SUM(n_calls) FROM investor_backfill_progress GROUP BY status"
        ).fetchall()
        total_calls = conn.execute("SELECT SUM(n_calls) FROM investor_backfill_progress").fetchone()[0] or 0
        n_rows = conn.execute("SELECT COUNT(*) FROM investor_trading_kis_detail").fetchone()[0]
    print(f"전체 대상(추정): ~{total}종목")
    done = 0
    for status, cnt, calls in rows:
        print(f"  {status}: {cnt}종목 (누적 콜 {calls or 0})")
        if status == "done":
            done = cnt
    print(f"총 누적 콜수: {total_calls}")
    print(f"investor_trading_kis_detail 행수: {n_rows}")
    if done and total:
        pct = done / total * 100
        print(f"진행률: {done}/{total} ({pct:.1f}%)")


def run(target_start: str, symbols: list, max_symbols: int, dry_run: bool):
    if max_symbols:
        symbols = symbols[:max_symbols]
    log.info("백필 대상: %d종목, 목표 시작일: %s", len(symbols), target_start)
    if dry_run:
        log.info("[DRY-RUN] 대상만 출력: %s", symbols[:10])
        return

    token = get_access_token()
    t0 = time.time()
    n_total = len(symbols)
    n_finished = 0
    calls_so_far = 0

    def _job(sym):
        return backfill_one_symbol(token, sym, target_start, REQUEST_DELAY)

    results = []
    failed = []
    time_limited = []
    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
        futures = {pool.submit(_job, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = {"symbol": sym, "status": "failed", "n_calls": 0, "error": str(exc)}
            results.append(r)
            n_finished += 1
            calls_so_far += r.get("n_calls", 0)
            elapsed = time.time() - t0
            rate = calls_so_far / elapsed if elapsed > 0 else 0
            avg_calls_per_symbol = calls_so_far / n_finished if n_finished else 44
            remaining_symbols = n_total - n_finished
            eta_sec = (remaining_symbols * avg_calls_per_symbol) / rate if rate > 0 else 0
            if r["status"] == "failed":
                failed.append(sym)
            elif r["status"] == "time_limit":
                time_limited.append(sym)
            log.info(
                "[%d/%d] %s -> %s (calls=%d, %.2f콜/초, 잔여예상 %.1f시간)",
                n_finished, n_total, sym, r["status"], r.get("n_calls", 0), rate, eta_sec / 3600,
            )

    if time_limited:
        log.warning(
            "TIME LIMIT(시간대 제한) %d건 — 같은 실행 내 재시도는 무의미해 건너뜀. "
            "체크포인트는 남아있어 다음 실행 때 자동 재개됨: %s",
            len(time_limited), time_limited,
        )

    retry_round = 0
    while failed and retry_round < MAX_RETRY_ROUNDS:
        retry_round += 1
        log.info("실패 %d건 -> %d차 재시도(동시성1, 지연%.0fs)", len(failed), retry_round, RETRY_DELAY)
        time.sleep(3)
        still_failed = []
        with ThreadPoolExecutor(max_workers=RETRY_WORKERS) as pool:
            futures = {pool.submit(backfill_one_symbol, token, s, target_start, RETRY_DELAY): s for s in failed}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    r = fut.result()
                except Exception as exc:
                    r = {"symbol": sym, "status": "failed", "n_calls": 0, "error": str(exc)}
                results.append(r)
                if r["status"] == "failed":
                    still_failed.append(sym)
                elif r["status"] == "time_limit":
                    time_limited.append(sym)
                log.info("  [재시도] %s -> %s (calls=%d)", r["symbol"], r["status"], r.get("n_calls", 0))
        failed = still_failed

    total_calls = sum(r.get("n_calls", 0) for r in results)
    n_done = sum(1 for r in results if r["status"] in ("done", "already_done"))
    n_no_more = sum(1 for r in results if r["status"] in ("no_more_data", "already_no_more_data"))
    elapsed = time.time() - t0
    log.info(
        "백필 완료: done=%d, no_more_data=%d, time_limit=%d, 최종실패=%d, 총 콜수=%d, 소요=%.0fs (%.2f콜/초)",
        n_done, n_no_more, len(time_limited), len(failed), total_calls,
        elapsed, total_calls / elapsed if elapsed > 0 else 0,
    )
    if failed:
        log.warning("최종 실패 종목(시간제한 외 진짜 에러): %s", failed)
    if time_limited:
        log.warning("TIME LIMIT으로 남은 종목(다음 실행 때 재개): %s", time_limited)


def main():
    parser = argparse.ArgumentParser(description="KIS investor-trade-by-stock-daily 다년치 백필")
    parser.add_argument("--target-start", type=str, default="20210101", help="백필 목표 시작일 YYYYMMDD")
    parser.add_argument("--symbols", type=str, default=None, help="쉼표구분 종목코드(테스트용)")
    parser.add_argument("--max-symbols", type=int, default=0, help="0=전체")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true", help="진행상황만 출력하고 종료")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if not APP_KEY or not APP_SECRET:
        log.error("KIS_APP_KEY / KIS_APP_SECRET이 .env에 없습니다.")
        sys.exit(1)

    limit_symbols = args.symbols.split(",") if args.symbols else None
    symbols = load_target_symbols(limit_symbols)
    run(args.target_start, symbols, args.max_symbols, args.dry_run)


if __name__ == "__main__":
    main()
