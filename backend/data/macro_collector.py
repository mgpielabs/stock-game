"""매크로 지표 수집 — yfinance(환율/변동성/금리/원자재) + 내부 DB(KOSPI/KOSDAQ/수급)"""
import argparse
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "stocks.db"

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

YFINANCE_TICKERS: dict[str, str] = {
    "usd_krw":  "KRW=X",
    "vix":      "^VIX",
    "us10y":    "^TNX",
    "wti":      "CL=F",
    "jpy_krw":  "JPYKRW=X",
}


def _collect_yfinance(days_back: int = 7, period: str | None = None) -> list[tuple[str, str, float]]:
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance 미설치 — 외부 지표 수집 스킵")
        return []

    rows: list[tuple[str, str, float]] = []

    for indicator, ticker in YFINANCE_TICKERS.items():
        try:
            if period:
                df = yf.download(
                    ticker,
                    period=period,
                    progress=False,
                    auto_adjust=True,
                )
            else:
                end = date.today()
                start = end - timedelta(days=days_back + 15)
                df = yf.download(
                    ticker,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    progress=False,
                    auto_adjust=True,
                )
            if df.empty:
                logger.warning(f"yfinance {ticker}: 빈 응답")
                continue
            for idx in df.index:
                # yfinance 1.6.x MultiIndex 포맷: ('Close', ticker)
                try:
                    col = ("Close", ticker)
                    val = float(df[col].loc[idx])
                except (KeyError, TypeError):
                    try:
                        val = float(df["Close"].loc[idx])
                    except Exception:
                        continue
                if val != val:  # NaN
                    continue
                dt = idx.strftime("%Y%m%d")
                rows.append((dt, indicator, val))
        except Exception as e:
            logger.warning(f"yfinance {ticker}: {e}")

    logger.info(f"yfinance 수집: {len(rows)}행")
    return rows


def _collect_from_db(limit: int = 260, use_detail: bool = False) -> list[tuple[str, str, float]]:
    rows: list[tuple[str, str, float]] = []
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            # KOSPI (code=1001)
            for dt, close in conn.execute(
                f"SELECT date, close FROM market_index WHERE code='1001' ORDER BY date DESC LIMIT {limit}"
            ):
                rows.append((dt, "kospi", float(close)))

            # KOSDAQ (code=2001)
            for dt, close in conn.execute(
                f"SELECT date, close FROM market_index WHERE code='2001' ORDER BY date DESC LIMIT {limit}"
            ):
                rows.append((dt, "kosdaq", float(close)))

            # 외국인/기관 순매수
            flow_table = "investor_trading_kis_detail" if use_detail else "investor_trading_kis"
            try:
                if use_detail:
                    frgn_col, inst_col = "foreign_value", "inst_total_value"
                else:
                    frgn_col, inst_col = "foreign_net_value", "inst_net_value"
                for dt, frgn, inst in conn.execute(
                    f"""SELECT date, SUM({frgn_col}), SUM({inst_col})
                       FROM {flow_table}
                       GROUP BY date
                       ORDER BY date DESC LIMIT {limit}"""
                ):
                    if frgn is not None:
                        rows.append((dt, "foreign_net", float(frgn)))
                    if inst is not None:
                        rows.append((dt, "inst_net", float(inst)))
            except Exception as e:
                logger.warning(f"{flow_table} 집계 실패: {e}")

    except Exception as e:
        logger.warning(f"DB 수집 실패: {e}")

    logger.info(f"DB 내부 수집: {len(rows)}행")
    return rows


def _upsert(rows: list[tuple[str, str, float]], skip_existing: bool = False) -> None:
    if not rows:
        return
    sql = (
        "INSERT OR IGNORE INTO macro_indicators (date, indicator, value) VALUES (?,?,?)"
        if skip_existing
        else "INSERT OR REPLACE INTO macro_indicators (date, indicator, value) VALUES (?,?,?)"
    )
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executemany(sql, rows)
        conn.commit()


def run(days_back: int = 7, skip_existing: bool = False) -> None:
    rows = _collect_yfinance(days_back=days_back) + _collect_from_db()
    _upsert(rows, skip_existing=skip_existing)
    logger.info(f"매크로 지표 총 {len(rows)}행 upsert 완료")


def run_backfill(days: int = 730, yf_period: str | None = None) -> None:
    """최근 N일치 yfinance 데이터 백필 (기존 날짜 스킵, DB 내부 데이터는 detail 테이블 활용)"""
    if yf_period:
        logger.info(f"매크로 백필 시작: yfinance period={yf_period}")
        yf_rows = _collect_yfinance(period=yf_period)
    else:
        logger.info(f"매크로 백필 시작: 최근 {days}일")
        yf_rows = _collect_yfinance(days_back=days)
    db_rows = _collect_from_db(limit=max(days, 1300), use_detail=True)
    all_rows = yf_rows + db_rows
    _upsert(all_rows, skip_existing=True)
    logger.info(f"매크로 백필 완료: {len(all_rows)}행 처리 (기존 날짜 스킵)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="매크로 지표 수집")
    parser.add_argument("--backfill", action="store_true", help="yfinance 5년치 백필 (기존 날짜 스킵)")
    parser.add_argument("--days", type=int, default=7, help="수집 기간(일). --backfill 시 yfinance는 5y 고정")
    args = parser.parse_args()

    if args.backfill:
        run_backfill(days=args.days, yf_period='5y')
    else:
        run(days_back=args.days)
