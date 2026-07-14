"""과거 데이터 백필: 멀티프로세싱 없이 직접 pykrx 호출 (스레드 안전)"""
import sys, sqlite3, logging, time
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent / "data"))
from db import DB_PATH, transaction, insert_prices

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BACKFILL_START = "20210525"
BACKFILL_END   = "20230507"
THREAD_WORKERS = 4   # pykrx rate limit 고려해 낮게 설정
_LOCK = __import__("threading").Lock()


def fetch_pykrx_direct(symbol: str, start: str, end: str):
    """멀티프로세싱 없이 직접 pykrx 호출 — 스레드 내에서 실행"""
    try:
        from pykrx import stock
        return stock.get_market_ohlcv_by_date(start, end, symbol)
    except Exception as e:
        logger.debug("pykrx 오류 [%s]: %s", symbol, e)
        return None


def backfill_symbol(symbol: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        oldest = conn.execute(
            "SELECT MIN(date) FROM prices WHERE symbol = ?", (symbol,)
        ).fetchone()[0]

    if oldest and oldest <= BACKFILL_START:
        return 0  # 이미 충분한 과거 데이터 있음

    end = BACKFILL_END
    if oldest:
        prev = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        if prev < BACKFILL_START:
            return 0
        end = min(end, prev)

    df = fetch_pykrx_direct(symbol, BACKFILL_START, end)
    if df is None or df.empty:
        return 0

    records = []
    for dt, row in df.iterrows():
        records.append({
            "symbol": symbol,
            "date": dt.strftime("%Y%m%d"),
            "open": int(row.get("시가", 0) or 0),
            "high": int(row.get("고가", 0) or 0),
            "low": int(row.get("저가", 0) or 0),
            "close": int(row.get("종가", 0) or 0),
            "volume": int(row.get("거래량", 0) or 0),
            "market_cap": None,
        })

    if records:
        insert_prices(records)
    return len(records)


def main():
    with sqlite3.connect(DB_PATH) as conn:
        symbols = [r[0] for r in conn.execute(
            "SELECT symbol FROM (SELECT symbol, MIN(date) as oldest FROM prices GROUP BY symbol) WHERE oldest > ?",
            (BACKFILL_START,)
        ).fetchall()]

    logger.info("백필 대상: %d종목 (%s ~ %s)", len(symbols), BACKFILL_START, BACKFILL_END)

    total_inserted = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
        futures = {pool.submit(backfill_symbol, sym): sym for sym in symbols}
        for future in tqdm(as_completed(futures), total=len(futures), desc="백필", unit="종목"):
            sym = futures[future]
            try:
                n = future.result()
                if n > 0:
                    total_inserted += n
                else:
                    skipped += 1
            except Exception as e:
                logger.error("오류 [%s]: %s", sym, e)

    logger.info("완료: 삽입 레코드=%d, 데이터없음=%d", total_inserted, skipped)

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM prices").fetchone()
        logger.info("DB 현황: %s ~ %s, 총 %d행", *row)


if __name__ == "__main__":
    main()
