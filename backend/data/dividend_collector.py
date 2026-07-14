"""
배당 데이터 수집기 — DART 사업보고서 "배당에 관한 사항"(alotMatter) 기준

배경:
  pykrx의 EPS/BPS/DIV/DPS API(get_market_fundamental*)가 2026-06 확인 기준
  KRX 로그인을 요구하도록 막혀 있어(비공식 API 정책 변경으로 추정) 사용 불가.
  대신 이미 이 프로젝트가 키를 보유한 DART Open API의 alotMatter.json으로
  사업연도별(연 1회) 주당배당금(DPS)/현금배당수익률/배당성향/EPS를 수집한다.

  법인(corp_code) 단위로 호출 — 보통주/우선주가 같은 법인이면 한 번의 응답에
  둘 다 포함돼 있어, 종목코드 수보다 실제 호출 수가 적다
  (disclosure_filter.py의 우선주→보통주 변환 패턴 재사용).

  배당락일(ex_dividend_date)/배당기준일(record_date)은 공시에 명시값이 없어
  결산일(stlm_dt) 기준으로 거래일 캘린더(market_index)에서 계산한 근사값이다
  — "결산일 당일 또는 그 직전 거래일=배당기준일, 그 1거래일 전=배당락일" 규칙.

게임 등 포그라운드 작업 방해 안 하도록 자기 프로세스 우선순위를 IDLE로 낮춤(Windows).

실행:
  python dividend_collector.py                          # 최근 5개 사업연도, 전종목
  python dividend_collector.py --years 3                 # 최근 3개 사업연도
  python dividend_collector.py --symbols 005930,000660   # 특정 종목만 (디버그용)
  python dividend_collector.py --dry-run                 # 대상 목록/호출 횟수만 출력
"""

import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# 콘솔/게임 등 포그라운드 작업에 영향 안 주도록 우선순위 최저로 (Windows)
if sys.platform == "win32":
    import ctypes
    IDLE_PRIORITY_CLASS = 0x00000040
    try:
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), IDLE_PRIORITY_CLASS
        )
    except Exception:
        pass

# daily_pipeline.py 등 자식 프로세스 출력을 UTF-8로 디코딩하는 경우가 있어 맞춰줌 —
# 콘솔이 cp949인 환경(.bat 등)에서 안 맞추면 UnicodeEncodeError 스팸 발생(2026-06-23)
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
        logging.FileHandler("dividend_collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# 2026-06-23: bps_collector.py와 동일한 burst 오판 버그 발견 후 동일 안전장치 적용
# — 연결예외와 "정상응답+데이터없음"을 구분 못 해 213종목(5.4%)이라는 비정상적으로 낮은
# 배당 지급률이 나왔을 가능성. 동시성 8→3, 지연 0.2초, 재시도(dart_get)로 완화.
THREAD_WORKERS = 3
REQUEST_DELAY = 0.2
DEFAULT_YEARS = 5
REPORT_CODE = "11011"    # 사업보고서(연간)
CONSECUTIVE_FAIL_LIMIT = 30  # 연속 "진짜 연결 실패" 시 한도 도달로 판단하고 중단

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)


def _to_common_stock(code: str) -> str:
    """우선주 코드를 보통주 코드로 변환 (DART corp_code는 법인 단위 — disclosure_filter.py와 동일 패턴)."""
    if len(code) == 6 and code[-1] in "579":
        return code[:-1] + "0"
    return code


def _parse_num(s: Optional[str]):
    if s is None or s == "-" or str(s).strip() == "":
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def fetch_alot_matter(corp_code: str, biz_year: int) -> List[Dict]:
    """DART alotMatter.json 호출. 상태 비정상(데이터없음 등 정상 응답)이면 빈 리스트를 반환
    하지만, 연결오류는 dart_get() 재시도까지 실패하면 예외를 그대로 전파시켜 run()이
    "진짜 연결 실패"와 "정상 응답인데 배당 없는 회사"를 구분할 수 있게 함(2026-06-23,
    bps_collector.py와 동일한 버그 수정 — 둘을 구분 못 해 213종목/5.4%라는 비정상적으로
    낮은 배당지급률이 나왔을 가능성이 있었음)."""
    time.sleep(REQUEST_DELAY)
    data = dart_get(
        "https://opendart.fss.or.kr/api/alotMatter.json",
        {
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": str(biz_year),
            "reprt_code": REPORT_CODE,
        },
    )
    if data.get("status") != "000":
        return []
    return data.get("list", [])


def parse_dividend_record(items: List[Dict]) -> Dict:
    """alotMatter 응답을 보통주/우선주 DPS·배당수익률 + 공통 항목으로 정리."""
    result: Dict = {
        "common_dps": None, "common_yield": None,
        "preferred_dps": None, "preferred_yield": None,
        "payout_ratio": None, "eps": None, "par_value": None,
        "settlement_date": None,
    }
    for item in items:
        se = item.get("se", "") or ""
        knd = item.get("stock_knd", "") or ""
        val = _parse_num(item.get("thstrm"))
        if item.get("stlm_dt") and not result["settlement_date"]:
            result["settlement_date"] = item["stlm_dt"].replace("-", "")

        if "주당 현금배당금" in se:
            if knd == "보통주":
                result["common_dps"] = val
            elif knd == "우선주":
                result["preferred_dps"] = val
        elif "현금배당수익률" in se:
            if knd == "보통주":
                result["common_yield"] = val
            elif knd == "우선주":
                result["preferred_yield"] = val
        elif "현금배당성향" in se:
            result["payout_ratio"] = val
        elif "주당순이익" in se and "연결" in se:
            result["eps"] = val
        elif se == "주당액면가액(원)":
            result["par_value"] = val
    return result


def calc_dividend_dates(conn: sqlite3.Connection, settlement_date: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """결산일 기준 배당기준일(가장 가까운 직전 거래일)과 배당락일(그 1거래일 전) 계산.
    market_index(KOSPI=1001) 거래일 캘린더 사용 — 결산일이 휴장일이어도 안전하게 처리."""
    if not settlement_date:
        return None, None
    rows = conn.execute(
        "SELECT date FROM market_index WHERE code='1001' AND date <= ? ORDER BY date DESC LIMIT 2",
        (settlement_date,),
    ).fetchall()
    if not rows:
        return None, None
    record_date = rows[0][0]
    ex_date = rows[1][0] if len(rows) > 1 else None
    return record_date, ex_date


def build_targets(symbols_filter: Optional[List[str]]) -> Dict[str, List[Tuple[str, str]]]:
    """corp_code -> [(symbol, stock_type), ...] 매핑 구성. ETF 제외, DART 미등록 종목 제외."""
    with get_connection() as conn:
        rows = conn.execute("SELECT symbol, name FROM stocks").fetchall()

    cmap = load_corp_code_map()
    targets: Dict[str, List[Tuple[str, str]]] = {}
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
        stock_type = "우선주" if common != symbol else "보통주"
        targets.setdefault(corp_code, []).append((symbol, stock_type))

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
            INSERT INTO dividends
                (symbol, biz_year, settlement_date, record_date, ex_dividend_date,
                 dps, dividend_yield, payout_ratio, eps, par_value, stock_type, corp_code)
            VALUES
                (:symbol, :biz_year, :settlement_date, :record_date, :ex_dividend_date,
                 :dps, :dividend_yield, :payout_ratio, :eps, :par_value, :stock_type, :corp_code)
            ON CONFLICT(symbol, biz_year) DO UPDATE SET
                settlement_date=excluded.settlement_date,
                record_date=excluded.record_date,
                ex_dividend_date=excluded.ex_dividend_date,
                dps=excluded.dps,
                dividend_yield=excluded.dividend_yield,
                payout_ratio=excluded.payout_ratio,
                eps=excluded.eps,
                par_value=excluded.par_value,
                stock_type=excluded.stock_type,
                corp_code=excluded.corp_code
            """,
            records,
        )


def run(years: int, symbols_filter: Optional[List[str]], dry_run: bool) -> None:
    init_db()
    targets = build_targets(symbols_filter)

    end_year = datetime.now().year - 1
    biz_years = list(range(end_year - years + 1, end_year + 1))
    logger.info("대상 사업연도: %s", biz_years)

    # 이미 dividends에 행이 있는 (symbol, biz_year)는 건너뜀 — 한 법인의 모든 심볼(보통/우선주)
    # 전부 행이 있어야 "완료"로 간주(2026-06-23, bps_collector.py와 동일 skip 패턴). 행이
    # 없는 건 "진짜 무배당"일 수도, "이전 burst 실패로 누락"일 수도 있어 재시도 대상으로 둠
    # — 이번 재수집의 핵심 목적이 바로 그 둘을 구분해내는 것.
    with get_connection() as conn:
        done_rows = conn.execute("SELECT DISTINCT symbol, biz_year FROM dividends").fetchall()
    done_by_year: Dict[int, set] = {}
    for sym, yr in done_rows:
        done_by_year.setdefault(yr, set()).add(sym)

    all_jobs = [(corp_code, year) for corp_code in targets for year in biz_years]
    jobs = [
        (c, y) for c, y in all_jobs
        if not all(sym in done_by_year.get(y, set()) for sym, _ in targets[c])
    ]
    logger.info(
        "전체 %d건 중 이미 완료 %d건 — 남은 %d건만 호출 (법인 %d개 x 연도 %d개)",
        len(all_jobs), len(all_jobs) - len(jobs), len(jobs), len(targets), len(biz_years),
    )

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
        items = fetch_alot_matter(corp_code, year)
        return (corp_code, year), (parse_dividend_record(items) if items else None)

    t0 = time.time()
    n_dividend_found, n_no_dividend, n_network_fail, consecutive_fail = 0, 0, 0, 0
    aborted = False
    pending_records: List[Dict] = []
    date_conn = get_connection()

    try:
        with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
            futures = {pool.submit(_job, c, y): (c, y) for c, y in jobs}
            pbar = tqdm(as_completed(futures), total=len(futures), desc="배당 수집", unit="건")
            for fut in pbar:
                corp_code, year = futures[fut]
                try:
                    (_, _), parsed = fut.result()
                except Exception as exc:
                    # 진짜 연결 실패(dart_get 재시도까지 다 실패) — DART 한도 도달 판단의
                    # 유일한 근거. "정상 응답인데 배당 없음"(parsed is None)과는 구분해야 함.
                    logger.debug("호출 실패 corp_code=%s year=%s: %s", corp_code, year, exc)
                    n_network_fail += 1
                    consecutive_fail += 1
                    if consecutive_fail >= CONSECUTIVE_FAIL_LIMIT:
                        logger.warning(
                            "연속 연결 실패 %d건 — DART 한도 도달로 판단, 남은 작업 취소하고 중단",
                            consecutive_fail,
                        )
                        aborted = True
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    continue

                consecutive_fail = 0
                if parsed is None:
                    n_no_dividend += 1
                    continue

                n_dividend_found += 1
                record_date, ex_date = calc_dividend_dates(date_conn, parsed["settlement_date"])
                for symbol, stock_type in targets[corp_code]:
                    dps = parsed["common_dps"] if stock_type == "보통주" else parsed["preferred_dps"]
                    div_yield = parsed["common_yield"] if stock_type == "보통주" else parsed["preferred_yield"]
                    pending_records.append({
                        "symbol": symbol, "biz_year": year,
                        "settlement_date": parsed["settlement_date"],
                        "record_date": record_date, "ex_dividend_date": ex_date,
                        "dps": dps, "dividend_yield": div_yield,
                        "payout_ratio": parsed["payout_ratio"], "eps": parsed["eps"],
                        "par_value": parsed["par_value"], "stock_type": stock_type,
                        "corp_code": corp_code,
                    })

                if len(pending_records) >= 100:
                    _save_batch(pending_records)
                    pending_records = []
    finally:
        date_conn.close()

    _save_batch(pending_records)

    elapsed = time.time() - t0
    logger.info(
        "수집 종료 | 배당확인 %d | 배당없음 %d | 연결실패 %d | 처리 %d/%d (%.0fs) | %s",
        n_dividend_found, n_no_dividend, n_network_fail,
        n_dividend_found + n_no_dividend + n_network_fail, len(jobs), elapsed,
        "한도로 중단됨" if aborted else "전체 완료",
    )

    with sqlite3.connect(Path(__file__).parent / "stocks.db") as conn:
        n_rows = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]
        n_syms = conn.execute("SELECT COUNT(DISTINCT symbol) FROM dividends").fetchone()[0]
    logger.info("dividends 테이블 현황: %d행, %d종목", n_rows, n_syms)


def main() -> None:
    parser = argparse.ArgumentParser(description="DART 배당 데이터 수집기")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS, help="수집할 과거 사업연도 수")
    parser.add_argument("--symbols", type=str, default=None, help="쉼표구분 종목코드 (디버그용)")
    parser.add_argument("--dry-run", action="store_true", help="대상/호출횟수만 출력하고 종료")
    args = parser.parse_args()

    symbols_filter = args.symbols.split(",") if args.symbols else None
    run(years=args.years, symbols_filter=symbols_filter, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
