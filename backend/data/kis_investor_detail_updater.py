"""
KIS investor-trade-by-stock-daily(FHPTJ04160001) 기반 investor_trading_kis_detail 일별 증분 갱신.

백필(kis_investor_backfill.py)은 과거를 채우는 용도이고, 이 스크립트는 매일 최신 데이터를
채우는 용도다. 한 번의 API 호출이 앵커 기준 직전 ~30거래일을 반환하므로, 각 종목마다
최신 거래일을 앵커로 한 번만 호출하면 최근 누락 데이터가 모두 채워진다.

TIME LIMIT 제한: 이 TR(FHPTJ04160001)은 00:00~15:40 KST엔 응답하지 않음.
daily_pipeline 16:30 실행이라 정상 동작하지만, 수동 실행 시 15:40 이후에 호출할 것.

실행:
  python kis_investor_detail_updater.py [--max-jobs 400] [--stale-days 7] [--dry-run]
  python kis_investor_detail_updater.py --status
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    BACKEND_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BACKEND_DIR / ".env")
except ImportError:
    BACKEND_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).parent))
from db import DB_PATH, get_connection, insert_investor_trading_kis_detail  # noqa: E402
from kis_investor_backfill import (  # noqa: E402
    ETF_PATTERN, fetch_batch, parse_rows, _latest_trading_day,
)
from kis_auth import get_access_token  # noqa: E402

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")

REQUEST_DELAY = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BACKEND_DIR / "kis_investor_detail_updater.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def _load_stale_symbols(stale_days: int) -> list:
    """stale_days 이상 갱신이 안 된 종목 목록. 가장 오래된 순으로 반환."""
    with get_connection() as conn:
        # 현재 per-symbol 최신 날짜
        latest_per_sym = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT symbol, MAX(date) FROM investor_trading_kis_detail GROUP BY symbol"
            ).fetchall()
        }

        # 거래대금 큰 순으로 전체 종목 목록
        names = {sym: name for sym, name in conn.execute("SELECT symbol, name FROM stocks").fetchall()}
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
        all_syms = ranked + sorted(s for s in names if s not in ranked_set)
        # ETF/인버스 제외
        target = [s for s in all_syms if not ETF_PATTERN.search(names.get(s, "") or "")]

    latest_trading = _latest_trading_day()

    def _days_stale(sym: str) -> int:
        """symbols의 마지막 날짜와 latest_trading_day 사이 날짜 차이(달력일)."""
        last = latest_per_sym.get(sym)
        if not last:
            return 9999
        # 날짜 문자열 비교 (YYYYMMDD 형식 → 정수 비교 가능)
        if last >= latest_trading:
            return 0
        # 간단히 달력일 차이 (정확한 거래일 계산 없이 충분)
        from datetime import datetime
        d1 = datetime.strptime(last, "%Y%m%d")
        d2 = datetime.strptime(latest_trading, "%Y%m%d")
        return (d2 - d1).days

    stale = [(s, _days_stale(s)) for s in target]
    stale = [(s, d) for s, d in stale if d >= stale_days]
    stale.sort(key=lambda x: -x[1])  # 가장 오래된 순
    return [s for s, _ in stale]


def update_one_symbol(token: str, symbol: str, anchor: str) -> dict:
    """앵커 날짜 기준 최근 ~30거래일 데이터를 한 번 호출해 upsert."""
    time.sleep(REQUEST_DELAY)
    try:
        output2 = fetch_batch(token, symbol, anchor)
    except Exception as exc:
        err = str(exc)
        status = "time_limit" if "TIME LIMIT" in err else "failed"
        return {"symbol": symbol, "status": status, "n_inserted": 0, "error": err}

    if not output2:
        return {"symbol": symbol, "status": "no_data", "n_inserted": 0}

    records = parse_rows(symbol, output2)
    insert_investor_trading_kis_detail(records)
    return {"symbol": symbol, "status": "ok", "n_inserted": len(records)}


def show_status():
    with get_connection() as conn:
        latest = conn.execute(
            "SELECT MAX(date) FROM investor_trading_kis_detail"
        ).fetchone()[0]
        pension_latest = conn.execute(
            "SELECT MAX(date) FROM investor_trading_kis_detail"
            " WHERE pension_value IS NOT NULL AND pension_value != 0"
        ).fetchone()[0]
        n_rows = conn.execute("SELECT COUNT(*) FROM investor_trading_kis_detail").fetchone()[0]
        n_syms = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM investor_trading_kis_detail"
        ).fetchone()[0]
    latest_trading = _latest_trading_day()
    print(f"investor_trading_kis_detail: {n_rows}행, {n_syms}종목")
    print(f"  전체 최신 날짜: {latest}  (prices 최신: {latest_trading})")
    print(f"  연기금(pension) 최신 날짜: {pension_latest}")


def run(max_jobs: int, stale_days: int, dry_run: bool):
    log.info("=== investor_trading_kis_detail 일별 갱신 시작 (stale_days=%d, max_jobs=%d) ===",
             stale_days, max_jobs)

    stale_syms = _load_stale_symbols(stale_days)
    if not stale_syms:
        log.info("갱신 필요 종목 없음 (전체 최신 상태)")
        return

    target = stale_syms[:max_jobs]
    anchor = _latest_trading_day()
    log.info("갱신 대상: %d종목 (전체 stale %d종목), anchor=%s",
             len(target), len(stale_syms), anchor)

    if dry_run:
        log.info("[DRY-RUN] 대상 종목(처음 10개): %s", target[:10])
        return

    token = get_access_token()
    n_ok = n_fail = n_time_limit = 0
    time_limit_hit = False

    for i, sym in enumerate(target, 1):
        r = update_one_symbol(token, sym, anchor)
        if r["status"] == "ok":
            n_ok += 1
        elif r["status"] == "time_limit":
            n_time_limit += 1
            time_limit_hit = True
            log.warning(
                "[%d/%d] %s -> TIME LIMIT (15:40 이전 실행 — 나머지 %d건 건너뜀)",
                i, len(target), sym, len(target) - i,
            )
            break  # TIME LIMIT는 시간 기반이라 나머지도 전부 실패 → 조기 종료
        elif r["status"] == "no_data":
            n_ok += 1  # 데이터 없음도 정상 (상장 전 등)
        else:
            n_fail += 1
            log.warning("[%d/%d] %s -> %s: %s", i, len(target), sym, r["status"], r.get("error", ""))

        if i % 100 == 0:
            log.info("[%d/%d] ok=%d fail=%d time_limit=%d",
                     i, len(target), n_ok, n_fail, n_time_limit)

    remaining = len(stale_syms) - len(target)
    log.info(
        "완료: ok=%d fail=%d time_limit=%d | 남은 stale 종목=%d%s",
        n_ok, n_fail, n_time_limit, remaining,
        " (내일 계속)" if remaining > 0 or time_limit_hit else "",
    )


def main():
    parser = argparse.ArgumentParser(description="investor_trading_kis_detail 일별 증분 갱신")
    parser.add_argument("--max-jobs", type=int, default=400,
                        help="최대 API 호출 수 (기본 400 ≈ 7분)")
    parser.add_argument("--stale-days", type=int, default=7,
                        help="N일 이상 갱신 안 된 종목만 대상 (기본 7)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true", help="현황만 출력")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    try:
        run(args.max_jobs, args.stale_days, args.dry_run)
    except Exception as exc:
        log.exception("갱신 중 예외: %s", exc)
    sys.exit(0)  # 항상 exit 0 — 파이프라인 전체를 막지 않음


if __name__ == "__main__":
    main()
