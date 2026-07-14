"""
BPS(주당순자산) 수집기 — DART 재무제표(자본총계) + 주식총수 현황

배경: 저PBR 스크리너 필터가 BPS 데이터가 없어 ❓ 미검증 상태(backend/ml/per_validation.py로
저PER을 EPS 역산해 검증한 것과 동일한 방식을 PBR에도 적용하려는 시도). DART alotMatter(배당
수집기가 쓰는 API)엔 BPS가 없어 신규 엔드포인트 2개가 필요:
  1. fnlttSinglAcntAll.json (전체 재무제표, 연결기준) — 자본총계
  2. stockTotqySttus.json (주식의 총수 현황) — 보통주+우선주 발행주식수
법인(corp_code)당 사업연도마다 2회 호출 — 배당 수집기보다 호출량이 2배라 DART 일일 한도에
걸릴 위험이 큼. 그래서 매 (corp_code, year) 결과를 받는 즉시 DB에 저장(끊겨도 그동안 받은
건 보존)하고, 연속 실패가 감지되면(한도 추정) 남은 작업을 취소하고 즉시 종료.

게임 등 포그라운드 작업 방해 안 하도록 자기 프로세스 우선순위를 IDLE로 낮춤(Windows).

실행:
  python bps_collector.py                          # 최근 5개 사업연도, 전종목
  python bps_collector.py --years 3
  python bps_collector.py --symbols 005930,000660   # 디버그용
  python bps_collector.py --dry-run
"""

import argparse
import logging
import math
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040)
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from db import init_db, get_connection, transaction  # noqa: E402
from disclosure import DART_API_KEY, dart_get, load_corp_code_map  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),  # 기본값(stderr)은 위 reconfigure 효과를 못 받음
        logging.FileHandler("bps_collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

THREAD_WORKERS = 3       # sector_collector.py와 동일 (2026-06-22)
REQUEST_DELAY = 0.2      # 호출 사이 짧은 지연(초) — 회사당 2호출(equity+shares) 각각에 적용.
                         # (2026-06-22 정정: "연속 실패 30건 = DART 한도"로 보였던 게 실제로는
                         # run()의 안전장치가 "진짜 연결 실패"와 "정상 응답인데 데이터 없는 소형주"를
                         # 구분 못 해서였음(아래 fetch_equity/fetch_shares 주석 + run() 참고).
                         # 동시3/순차1, 0.2~1초 등 여러 조합으로 재현했지만 진짜 RemoteDisconnected는
                         # 한 번도 재현 안 됐고, 전부 이 오판이었음 — burst 차단 자체가 BPS 엔드포인트엔
                         # 해당 안 됐던 것으로 결론. 그래도 과거 disclosure.py 사례처럼 burst가 전혀
                         # 없다고 단정할 수 없어 sector와 같은 보수적인 값은 유지)
DEFAULT_YEARS = 5
REPORT_CODE = "11011"    # 사업보고서(연간)
CONSECUTIVE_FAIL_LIMIT = 30  # 연속 실패 시 한도 도달로 판단하고 중단

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)


def _to_common_stock(code: str) -> str:
    if len(code) == 6 and code[-1] in "579":
        return code[:-1] + "0"
    return code


def fetch_equity(corp_code: str, biz_year: int) -> Tuple[Optional[float], Optional[str]]:
    """연결기준 자본총계(원). 데이터 없음(status!=000)이면 (None, None) — 정상 응답이므로 예외를
    던지지 않음. 연결오류는 dart_get()이 짧게 재시도한 뒤에도 실패하면 그대로 예외를 전파시켜
    run()이 "진짜 연결 실패"와 "정상 응답인데 데이터가 없는 소형주"를 구분할 수 있게 함
    (2026-06-22: 둘을 구분 못 해 정상적인 데이터 없음 30건 연속을 DART 한도 초과로 오판하던
    버그 수정 — 작은 법인은 자본총계/주식총수 항목이 없는 게 흔해서 연속으로 몰릴 수 있음)."""
    time.sleep(REQUEST_DELAY)
    data = dart_get(
        "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
        {
            "crtfc_key": DART_API_KEY, "corp_code": corp_code,
            "bsns_year": str(biz_year), "reprt_code": REPORT_CODE, "fs_div": "CFS",
        },
    )
    if data.get("status") != "000":
        return None, None
    for item in data.get("list", []):
        if "자본총계" in (item.get("account_nm") or ""):
            try:
                equity = float(str(item.get("thstrm_amount", "")).replace(",", ""))
            except (ValueError, TypeError):
                continue
            settlement = (item.get("thstrm_dt") or "").replace("-", "")[:8] or None
            return equity, settlement
    return None, None


def fetch_shares(corp_code: str, biz_year: int) -> Optional[float]:
    """보통주+우선주 발행주식총수. 데이터 없음(status!=000)이면 None(정상 응답). 연결오류는
    dart_get() 재시도 후에도 실패하면 예외를 그대로 전파(위 fetch_equity 설명과 동일 이유)."""
    time.sleep(REQUEST_DELAY)
    data = dart_get(
        "https://opendart.fss.or.kr/api/stockTotqySttus.json",
        {
            "crtfc_key": DART_API_KEY, "corp_code": corp_code,
            "bsns_year": str(biz_year), "reprt_code": REPORT_CODE,
        },
    )
    if data.get("status") != "000":
        return None
    for item in data.get("list", []):
        if (item.get("se") or "").strip() == "합계":
            try:
                return float(str(item.get("istc_totqy", "")).replace(",", ""))
            except (ValueError, TypeError):
                return None
    return None


def fetch_bps(corp_code: str, biz_year: int) -> Dict:
    """둘 다 성공해야 bps 계산. 하나라도 실패하면 해당 필드만 None으로 부분 저장."""
    equity, settlement = fetch_equity(corp_code, biz_year)
    shares = fetch_shares(corp_code, biz_year)
    bps = (equity / shares) if (equity is not None and shares and shares > 0) else None
    return {"total_equity": equity, "shares_total": shares, "bps": bps, "settlement_date": settlement}


def build_targets(symbols_filter: Optional[List[str]]) -> Dict[str, List[str]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT symbol, name FROM stocks").fetchall()
    cmap = load_corp_code_map()
    targets: Dict[str, List[str]] = {}
    skipped_etf, skipped_unmapped = 0, 0
    for symbol, name in rows:
        if symbols_filter and symbol not in symbols_filter:
            continue
        if ETF_PATTERN.search(name or ""):
            skipped_etf += 1
            continue
        common = _to_common_stock(symbol)
        corp_code = cmap.get(common)
        if not corp_code:
            skipped_unmapped += 1
            continue
        targets.setdefault(corp_code, []).append(symbol)
    logger.info(
        "수집 대상: 법인 %d개 (종목 %d개) | ETF제외 %d | DART미등록제외 %d",
        len(targets), sum(len(v) for v in targets.values()), skipped_etf, skipped_unmapped,
    )
    return targets


def _save_batch(records: List[Dict]) -> None:
    if not records:
        return
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO financials (symbol, biz_year, settlement_date, total_equity, shares_total, bps, corp_code)
            VALUES (:symbol, :biz_year, :settlement_date, :total_equity, :shares_total, :bps, :corp_code)
            ON CONFLICT(symbol, biz_year) DO UPDATE SET
                settlement_date=excluded.settlement_date,
                total_equity=excluded.total_equity,
                shares_total=excluded.shares_total,
                bps=excluded.bps,
                corp_code=excluded.corp_code
            """,
            records,
        )


def run(years: int, symbols_filter: Optional[List[str]], dry_run: bool, max_jobs: Optional[int] = None) -> None:
    init_db()
    targets = build_targets(symbols_filter)

    end_year = datetime.now().year - 1
    biz_years = list(range(end_year - years + 1, end_year + 1))
    logger.info("대상 사업연도: %s", biz_years)

    # 이미 bps 확보된 (corp_code, biz_year)는 건너뜀 — DART 한도로 끊겼던 작업을 이어서 진행
    with get_connection() as conn:
        done_rows = conn.execute(
            "SELECT DISTINCT corp_code, biz_year FROM financials WHERE bps IS NOT NULL AND corp_code IS NOT NULL"
        ).fetchall()
    done = {(c, y) for c, y in done_rows}

    all_jobs = [(corp_code, year) for corp_code in targets for year in biz_years]
    jobs = [(c, y) for (c, y) in all_jobs if (c, y) not in done]
    logger.info(
        "전체 %d건 중 이미 완료 %d건 — 남은 %d건만 호출 (법인 %d개 x 연도 %d개, 회당 2엔드포인트 = %d회)",
        len(all_jobs), len(all_jobs) - len(jobs), len(jobs), len(targets), len(biz_years), len(jobs) * 2,
    )

    n_remaining = len(jobs)
    if max_jobs is not None and n_remaining > max_jobs:
        days_needed = math.ceil(n_remaining / max_jobs)
        logger.info(
            "--max-jobs %d 적용: 이번 실행은 %d건(%d회 호출)만 처리, 나머지 %d건은 다음 실행으로 "
            "(하루 1회 자동 실행 기준 약 %d일 더 소요 예상)",
            max_jobs, max_jobs, max_jobs * 2, n_remaining - max_jobs, days_needed,
        )
        jobs = jobs[:max_jobs]
    elif max_jobs is not None:
        logger.info("--max-jobs %d >= 남은 %d건 — 이번 실행으로 전부 완료 가능", max_jobs, n_remaining)

    if dry_run:
        logger.info("[DRY-RUN] 실제 수집은 하지 않고 종료")
        return
    if not jobs:
        logger.info("남은 작업 없음 — 이미 전부 수집됨")
        return
    if not DART_API_KEY:
        logger.error("DART_API_KEY가 설정되지 않음 — 수집 불가")
        return

    def _job(corp_code: str, year: int):
        return (corp_code, year), fetch_bps(corp_code, year)

    t0 = time.time()
    n_success, n_no_data, n_network_fail, consecutive_fail = 0, 0, 0, 0
    aborted = False
    pending_batch: List[Dict] = []

    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
        futures = {pool.submit(_job, c, y): (c, y) for c, y in jobs}
        pbar = tqdm(as_completed(futures), total=len(futures), desc="BPS 수집", unit="건")
        for fut in pbar:
            corp_code, year = futures[fut]
            try:
                (_, _), parsed = fut.result()
            except Exception as exc:
                # 진짜 연결 실패(dart_get 재시도까지 다 실패) — DART 한도 도달 판단의 근거가 되는
                # 유일한 케이스. "정상 응답인데 데이터 없음"(parsed.get("bps") is None)과는 구분해야 함
                # — 작은 법인은 자본총계/주식총수 항목이 없는 게 흔해서 그것만으로 한도 초과로
                # 오판하면 안 됨(2026-06-22 발견 — 실제로는 burst 차단이 아니라 이 오판이었음).
                logger.debug("호출 실패 corp_code=%s year=%s: %s", corp_code, year, exc)
                n_network_fail += 1
                consecutive_fail += 1
                continue

            consecutive_fail = 0
            if parsed.get("bps") is None:
                n_no_data += 1
            else:
                n_success += 1
                for symbol in targets[corp_code]:
                    pending_batch.append({
                        "symbol": symbol, "biz_year": year,
                        "settlement_date": parsed["settlement_date"],
                        "total_equity": parsed["total_equity"],
                        "shares_total": parsed["shares_total"],
                        "bps": parsed["bps"], "corp_code": corp_code,
                    })

            if len(pending_batch) >= 100:
                _save_batch(pending_batch)
                pending_batch = []

            if consecutive_fail >= CONSECUTIVE_FAIL_LIMIT:
                logger.warning(
                    "연속 연결 실패 %d건 — DART 한도 도달로 판단, 남은 작업 취소하고 중단 "
                    "(처리 %d/%d, 데이터없음은 연속실패로 안 침)",
                    consecutive_fail, n_success + n_no_data + n_network_fail, len(jobs),
                )
                aborted = True
                pool.shutdown(wait=False, cancel_futures=True)
                break

    _save_batch(pending_batch)

    elapsed = time.time() - t0
    logger.info(
        "수집 종료 | 성공 %d | 데이터없음 %d | 연결실패 %d | 처리 %d/%d (%.0fs) | %s",
        n_success, n_no_data, n_network_fail,
        n_success + n_no_data + n_network_fail, len(jobs), elapsed,
        "한도로 중단됨" if aborted else "전체 완료",
    )

    with sqlite3.connect(Path(__file__).parent / "stocks.db") as conn:
        n_rows = conn.execute("SELECT COUNT(*) FROM financials WHERE bps IS NOT NULL").fetchone()[0]
        n_syms = conn.execute("SELECT COUNT(DISTINCT symbol) FROM financials WHERE bps IS NOT NULL").fetchone()[0]
    logger.info("financials 테이블 현황: bps 있는 행 %d개, 종목 %d개", n_rows, n_syms)


def main() -> None:
    parser = argparse.ArgumentParser(description="DART BPS 수집기")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS, help="수집할 과거 사업연도 수")
    parser.add_argument("--symbols", type=str, default=None, help="쉼표구분 종목코드 (디버그용)")
    parser.add_argument("--dry-run", action="store_true", help="대상/호출횟수만 출력하고 종료")
    parser.add_argument(
        "--max-jobs", type=int, default=None,
        help="이번 실행에서 처리할 최대 (corp_code,year) 건수 — 매일 일부씩 나눠 받을 때 사용",
    )
    args = parser.parse_args()

    symbols_filter = args.symbols.split(",") if args.symbols else None
    run(years=args.years, symbols_filter=symbols_filter, dry_run=args.dry_run, max_jobs=args.max_jobs)


if __name__ == "__main__":
    main()
