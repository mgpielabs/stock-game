"""매크로 지표 수집 — yfinance(환율/변동성/금리/원자재) + 내부 DB(KOSPI/KOSDAQ/수급)"""
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


def _collect_yfinance(days_back: int = 7) -> list[tuple[str, str, float]]:
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance 미설치 — 외부 지표 수집 스킵")
        return []

    end = date.today()
    start = end - timedelta(days=days_back + 15)
    rows: list[tuple[str, str, float]] = []

    for indicator, ticker in YFINANCE_TICKERS.items():
        try:
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


def _collect_from_db() -> list[tuple[str, str, float]]:
    rows: list[tuple[str, str, float]] = []
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            # KOSPI (code=1001)
            for dt, close in conn.execute(
                "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date DESC LIMIT 260"
            ):
                rows.append((dt, "kospi", float(close)))

            # KOSDAQ (code=2001)
            for dt, close in conn.execute(
                "SELECT date, close FROM market_index WHERE code='2001' ORDER BY date DESC LIMIT 260"
            ):
                rows.append((dt, "kosdaq", float(close)))

            # 외국인/기관 순매수 (investor_trading_kis 일별 집계)
            try:
                for dt, frgn, inst in conn.execute(
                    """SELECT date, SUM(foreign_net_value), SUM(inst_net_value)
                       FROM investor_trading_kis
                       GROUP BY date
                       ORDER BY date DESC LIMIT 260"""
                ):
                    if frgn is not None:
                        rows.append((dt, "foreign_net", float(frgn)))
                    if inst is not None:
                        rows.append((dt, "inst_net", float(inst)))
            except Exception as e:
                logger.warning(f"investor_trading_kis 집계 실패: {e}")

    except Exception as e:
        logger.warning(f"DB 수집 실패: {e}")

    logger.info(f"DB 내부 수집: {len(rows)}행")
    return rows


def _upsert(rows: list[tuple[str, str, float]]) -> None:
    if not rows:
        return
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO macro_indicators (date, indicator, value) VALUES (?,?,?)",
            rows,
        )
        conn.commit()


def run() -> None:
    rows = _collect_yfinance() + _collect_from_db()
    _upsert(rows)
    logger.info(f"매크로 지표 총 {len(rows)}행 upsert 완료")


if __name__ == "__main__":
    run()
