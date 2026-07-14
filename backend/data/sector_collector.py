"""
업종(섹터) 분류 수집기 — DART 기업개황(company.json)의 induty_code(KSIC 업종코드) 기준

배경:
  pykrx의 업종분류 API도 다른 fundamental API들처럼 KRX 로그인 요구로 막혀 있어 사용 불가.
  대신 이미 이 프로젝트가 키를 보유한 DART Open API의 company.json으로 법인별 업종코드를
  가져온다(공시 필터링에 쓰는 disclosure.py와 동일 DART_API_KEY/corp_code_map 재사용).

  배당 수집기(dividend_collector.py)와 동일하게 corp_code 단위로 호출 — 보통주/우선주가
  같은 법인이면 한 번의 응답을 그대로 공유(_to_common_stock() 변환).

  업종코드(induty_code)는 KSIC(한국표준산업분류) 코드로, 기업의 사업 내용이 바뀌지 않는 한
  거의 변하지 않는 정적 속성이라 — 배당/시세처럼 연도별로 수집하지 않고 stocks.sector에
  현재값 1회만 저장한다 (point-in-time 정확도보다 "있는 것"이 우선인 placeholder 대체).

게임 등 포그라운드 작업 방해 안 하도록 자기 프로세스 우선순위를 IDLE로 낮춤(Windows).

실행:
  python sector_collector.py                          # 전종목
  python sector_collector.py --symbols 005930,000660   # 특정 종목만 (디버그용)
  python sector_collector.py --dry-run                 # 대상 목록/호출 횟수만 출력
"""

import argparse
import logging
import math
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32":
    import ctypes
    IDLE_PRIORITY_CLASS = 0x00000040
    try:
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), IDLE_PRIORITY_CLASS
        )
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
        logging.FileHandler("sector_collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

THREAD_WORKERS = 3       # DART burst 차단(RemoteDisconnected) 완화용으로 8→3 (2026-06-22)
REQUEST_DELAY = 0.2      # 호출 사이 짧은 지연(초) — 동시성 완화와 함께 burst 패턴을 줄임

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)


def _to_common_stock(code: str) -> str:
    """우선주 코드를 보통주 코드로 변환 (DART corp_code는 법인 단위 — dividend_collector.py와 동일 패턴)."""
    if len(code) == 6 and code[-1] in "579":
        return code[:-1] + "0"
    return code


def fetch_induty_code(corp_code: str) -> Optional[str]:
    """DART company.json 호출. 실패/없음이면 None. 연결오류는 dart_get()이 짧게 재시도."""
    time.sleep(REQUEST_DELAY)
    try:
        data = dart_get(
            "https://opendart.fss.or.kr/api/company.json",
            {"crtfc_key": DART_API_KEY, "corp_code": corp_code},
        )
        if data.get("status") != "000":
            return None
        return data.get("induty_code") or None
    except Exception as e:
        logger.debug("company.json 실패 corp_code=%s: %s", corp_code, e)
        return None


def build_targets(symbols_filter: Optional[List[str]]) -> Dict[str, List[str]]:
    """corp_code -> [symbol, ...] 매핑 구성. ETF 제외, DART 미등록 종목 제외."""
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


def run(symbols_filter: Optional[List[str]], dry_run: bool, max_jobs: Optional[int] = None) -> None:
    init_db()
    targets = build_targets(symbols_filter)

    # 이미 sector가 채워진 종목이 있는 법인은 건너뜀 — DART 한도로 끊겼던 수집을
    # 이어서 진행 (BPS 수집기와 동일 패턴, 2026-06-22). 한 법인의 여러 종목(보통/우선주)
    # 은 같은 induty_code를 한 번에 적용하므로, 그중 하나라도 채워져 있으면 완료로 간주.
    with get_connection() as conn:
        done_symbols = {
            sym for (sym,) in conn.execute("SELECT symbol FROM stocks WHERE sector IS NOT NULL").fetchall()
        }
    all_targets = targets
    targets = {
        corp_code: symbols for corp_code, symbols in all_targets.items()
        if not any(sym in done_symbols for sym in symbols)
    }
    n_done = len(all_targets) - len(targets)
    logger.info(
        "전체 법인 %d개 중 이미 완료 %d개 — 남은 %d개만 호출",
        len(all_targets), n_done, len(targets),
    )

    n_remaining = len(targets)
    if max_jobs is not None and n_remaining > max_jobs:
        days_needed = math.ceil(n_remaining / max_jobs)
        logger.info(
            "--max-jobs %d 적용: 이번 실행은 %d개만 처리, 나머지 %d개는 다음 실행으로 "
            "(하루 1회 자동 실행 기준 약 %d일 더 소요 예상)",
            max_jobs, max_jobs, n_remaining - max_jobs, days_needed,
        )
        targets = dict(list(targets.items())[:max_jobs])
    elif max_jobs is not None:
        logger.info("--max-jobs %d >= 남은 %d개 — 이번 실행으로 전부 완료 가능", max_jobs, n_remaining)

    if dry_run:
        logger.info("[DRY-RUN] 실제 수집은 하지 않고 종료")
        return
    if not targets:
        logger.info("남은 작업 없음 — 이미 전부 수집됨")
        return

    if not DART_API_KEY:
        logger.error("DART_API_KEY가 설정되지 않음 — 수집 불가")
        return

    results: Dict[str, str] = {}

    def _job(corp_code: str):
        return corp_code, fetch_induty_code(corp_code)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
        futures = [pool.submit(_job, c) for c in targets]
        for fut in tqdm(as_completed(futures), total=len(futures), desc="업종 수집", unit="법인"):
            corp_code, induty = fut.result()
            if induty:
                results[corp_code] = induty
    logger.info("DART 호출 완료: %d/%d 법인 업종코드 확보 (%.0fs)", len(results), len(targets), time.time() - t0)

    records: List[Tuple[str, str]] = []
    for corp_code, induty in results.items():
        for symbol in targets[corp_code]:
            records.append((induty, symbol))

    with transaction() as conn:
        conn.executemany("UPDATE stocks SET sector = ? WHERE symbol = ?", records)
    logger.info("stocks.sector 업데이트 완료: %d종목", len(records))


def main() -> None:
    parser = argparse.ArgumentParser(description="DART 업종코드 수집기")
    parser.add_argument("--symbols", type=str, default=None, help="쉼표구분 종목코드 (디버그용)")
    parser.add_argument("--dry-run", action="store_true", help="대상/호출횟수만 출력하고 종료")
    parser.add_argument(
        "--max-jobs", type=int, default=None,
        help="이번 실행에서 처리할 최대 법인 수(=DART 호출 수) — 매일 일부씩 나눠 받을 때 사용",
    )
    args = parser.parse_args()

    symbols_filter = args.symbols.split(",") if args.symbols else None
    run(symbols_filter=symbols_filter, dry_run=args.dry_run, max_jobs=args.max_jobs)


if __name__ == "__main__":
    main()
