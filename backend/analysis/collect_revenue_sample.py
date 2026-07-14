"""소표본(100종목) revenue/operating_income/net_income 수집 — 중복성 측정 전용.
fnlttSinglAcntAll.json (IS 항목)에서 기존 bps_collector와 동일 엔드포인트로 파싱.
신규 DART 호출 없음 — 같은 API에서 IS 항목만 추가 추출.
"""
import os, sys, time, logging
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from data.db import get_connection
from data.disclosure import dart_get

DART_KEY   = os.getenv("DART_API_KEY")
REPORT_CODE = "11011"  # 사업보고서 (bps_collector와 동일)
REQUEST_DELAY = 0.3    # 초

# IS account_id 매핑
REVENUE_IDS = {"ifrs-full_Revenue", "dart_Revenue", "ifrs-full_NetRevenue"}
OP_INCOME_IDS = {"dart_OperatingIncomeLoss", "ifrs-full_OperatingIncome"}
NET_INCOME_IDS = {"ifrs-full_ProfitLoss", "dart_ProfitLoss"}


def _to_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


def fetch_income_stmt(corp_code: str, biz_year: int) -> dict:
    """fnlttSinglAcntAll에서 IS 항목 3개 파싱."""
    time.sleep(REQUEST_DELAY)
    data = dart_get(
        "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
        {
            "crtfc_key": DART_KEY,
            "corp_code": corp_code,
            "bsns_year": str(biz_year),
            "reprt_code": REPORT_CODE,
            "fs_div": "CFS",
        },
    )
    result = {"revenue": None, "operating_income": None, "net_income": None}
    if data.get("status") != "000":
        return result
    for item in data.get("list", []):
        if item.get("sj_div") != "IS":
            continue
        aid = item.get("account_id", "")
        amt = _to_float(item.get("thstrm_amount"))
        if aid in REVENUE_IDS and result["revenue"] is None:
            result["revenue"] = amt
        elif aid in OP_INCOME_IDS and result["operating_income"] is None:
            result["operating_income"] = amt
        elif aid in NET_INCOME_IDS and result["net_income"] is None:
            result["net_income"] = amt
    return result


def main():
    with get_connection() as conn:
        # dividends+financials 교집합 100종목 (EPS 있어야 중복성 비교 가능)
        rows = conn.execute("""
            SELECT DISTINCT f.symbol, f.corp_code, f.biz_year
            FROM financials f
            WHERE f.corp_code IS NOT NULL
              AND f.revenue IS NULL
              AND f.biz_year IN (2021, 2022)
              AND f.symbol IN (
                  SELECT DISTINCT symbol FROM dividends WHERE eps IS NOT NULL
              )
            ORDER BY f.symbol, f.biz_year
        """).fetchall()

    if not rows:
        logger.info("수집할 대상 없음 (이미 채워졌거나 dividends-eps 없음)")
        return

    # 고유 종목 수
    syms = set(r[0] for r in rows)
    logger.info("수집 대상: %d개 종목-연도 (%d종목)", len(rows), len(syms))

    n_ok = n_nodata = n_err = 0
    for idx, (symbol, corp_code, biz_year) in enumerate(rows, 1):
        try:
            result = fetch_income_stmt(corp_code, biz_year)
        except Exception as e:
            logger.warning("[%3d] %s/%d 연결 오류: %s", idx, symbol, biz_year, e)
            n_err += 1
            continue

        if all(v is None for v in result.values()):
            n_nodata += 1
        else:
            with get_connection() as conn:
                conn.execute("""
                    UPDATE financials
                    SET revenue=?, operating_income=?, net_income=?
                    WHERE symbol=? AND biz_year=?
                """, (
                    result["revenue"], result["operating_income"], result["net_income"],
                    symbol, biz_year,
                ))
            n_ok += 1

        if idx % 50 == 0 or idx == len(rows):
            logger.info("[%3d/%d] ok=%d nodata=%d err=%d",
                        idx, len(rows), n_ok, n_nodata, n_err)

    logger.info("완료: ok=%d, 데이터없음=%d, 오류=%d", n_ok, n_nodata, n_err)

    # 수집 결과 요약
    with get_connection() as conn:
        r = conn.execute("""
            SELECT
              COUNT(*) AS total,
              COUNT(revenue) AS rev,
              COUNT(operating_income) AS op,
              COUNT(net_income) AS ni
            FROM financials
            WHERE biz_year IN (2021, 2022)
              AND symbol IN (SELECT DISTINCT symbol FROM dividends WHERE eps IS NOT NULL)
        """).fetchone()
    logger.info("financials 현황 (2021+2022): 전체=%d, revenue=%d, op_income=%d, net_income=%d",
                r[0], r[1], r[2], r[3])

    # IS growth 가능 종목 수 확인 (연속 2년 ok)
    with get_connection() as conn:
        is_growth = conn.execute("""
            SELECT COUNT(*) FROM financials a
            JOIN financials b ON a.symbol=b.symbol AND b.biz_year=a.biz_year-1
            WHERE a.revenue IS NOT NULL AND b.revenue IS NOT NULL
              AND a.biz_year = 2022
        """).fetchone()[0]
    logger.info("IS growth 가능 종목 (biz_year=2022, 연속 2021+2022 ok): %d종목", is_growth)


if __name__ == "__main__":
    main()
